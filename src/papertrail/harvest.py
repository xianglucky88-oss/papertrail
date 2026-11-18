"""单一职责：多来源分页采集（游标 / 限速 / 缓存 / 部分失败恢复）。

关键不变量
==========

1. **游标只能跟着提交一起前进。** 采集器先在内存里算出"下一页边界"，
   然后调用 :func:`store.commit_harvest_page`：记录、版本状态、游标、
   请求边界、响应版本、页指纹和请求轨迹在同一个事务里提交。只有事务
   返回 ``committed=True``，采集器才更新内存游标。提交失败时游标原地
   不动，重启后仍从上一个已确认的恢复点开始 —— 不会越过未落库的论文。

2. **空页有两种含义，必须用分页证据区分。**
   * 空页 + 证据表明"还有更多"（存在 ``next`` 边界、``total`` 大于已见
     条数、``page < total_pages``）→ :class:`Outcome.EMPTY_ANOMALY`，
     按可重试失败处理，游标不前进，来源状态记为 ``partial``，下次可从
     同一位置继续；
   * 空页 + 证据表明"确实没有更多" → :class:`Outcome.EMPTY_END`，
     提交一个 ``status='complete'`` 的状态，来源正常结束。
   两者绝不会被混为一谈。

3. **没有更多数据 ≠ 请求失败。** ``complete`` 只由有证据的空页或枯竭的
   ``next`` 边界产生；超时 / HTTP 错误 / 提交失败只会产生 ``partial``
   或 ``failed``，并保留可继续的游标。可重试失败记为 ``partial``，不可
   重试的 4xx 记为 ``failed``。

4. **重复页与重放不重复写入。** 每页内容指纹落在 ``harvest_pages``；
   已提交过的页再次出现时标记 ``duplicate_page``，其记录整体跳过写入，
   写入次数为 0。``next`` 指回已访问游标时标记 ``pagination_loop``：
   本页数据照常提交，但游标不前进，状态如实标 ``partial``。

5. **响应字段漂移**通过别名表读取（``items/results/data`` 等），不因
   上游改名而丢页。

离线：本模块不访问网络。来源由 :class:`FixtureSource` 提供（读 fixtures
或内存脚本），可注入 ``clock`` / ``sleep`` 以验证限速与缓存过期。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional

from . import store as _store
from .models import Record

# ---------------------------------------------------------------------------
# 响应字段别名：上游改名 / 字段漂移时仍能取到同一语义
# ---------------------------------------------------------------------------

ITEM_KEYS = ("items", "results", "data", "records", "entries")
NEXT_KEYS = ("next_cursor", "next", "nextToken", "next_page_token", "nextPageToken")
TOTAL_KEYS = ("total", "totalResults", "total_results", "totalCount", "total_count")
TOTAL_PAGES_KEYS = ("total_pages", "totalPages", "page_count", "pageCount")
PAGE_KEYS = ("page", "page_number", "pageNumber", "current_page", "currentPage")
VERSION_KEYS = ("version", "schema_version", "api_version", "response_version")
DOI_KEYS = ("doi", "DOI", "Doi")
ID_KEYS = ("id", "ID", "arxiv_id", "external_id", "work_id")


class Outcome(str, Enum):
    """一次分页请求的显式结果。"""

    OK = "ok"
    EMPTY_END = "empty_end"          # 空页且证据表明没有更多数据
    EMPTY_ANOMALY = "empty_anomaly"  # 空页但证据表明还有更多 → 异常
    DUPLICATE = "duplicate_page"     # 内容与已提交页相同
    FAILED = "failed"                # 重试耗尽 / 不可重试错误
    COMMIT_FAILED = "commit_failed"  # 落库事务失败，游标不得前进
    PAGINATION_LOOP = "pagination_loop"  # next 边界成环，无法安全推进
    TRUNCATED = "truncated"          # 非空页但证据说还有数据、却无 next 边界


class SourceStatus(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"  # 有可继续的位置
    FAILED = "failed"


@dataclass
class Response:
    """一次 HTTP-ish 响应。``error`` 非空表示传输层失败（超时等）。"""

    status_code: Optional[int] = None
    body: Optional[dict] = None
    headers: dict = field(default_factory=dict)
    error: Optional[str] = None

    def ok(self) -> bool:
        return self.error is None and 200 <= int(self.status_code or 0) < 300


@dataclass
class Source:
    """采集来源的静态配置。

    ``fetch(cursor, page, attempt, timeout)`` 返回 :class:`Response`，可
    抛 ``TimeoutError`` 或任意异常（都会被归类为可重试传输失败）。
    """

    name: str
    rate_per_sec: float = 5.0
    timeout: float = 5.0
    max_retries: int = 2
    start_cursor: Optional[str] = None

    def fetch(self, cursor, page, attempt, timeout):  # pragma: no cover - 接口
        raise NotImplementedError


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    return "transport"


@dataclass
class FixtureSource(Source):
    """离线脚本化来源。

    ``pages``：``{cursor_key: body}``，``cursor_key`` 用 ``""`` 表示首页。
    ``errors``：``{cursor_key: [kind, ...]}``，列表下标对应第 1..n 次尝试；
    ``kind`` 支持 ``"timeout"`` / ``"http500"`` / ``"http429"`` / ``"http404"``
    或 ``"exc"``（抛 RuntimeError）。列表耗尽后恢复正常响应。
    """

    pages: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)

    def fetch(self, cursor, page, attempt, timeout):
        key = "" if cursor is None else str(cursor)
        scripted = list((self.errors or {}).get(key) or ())
        if attempt <= len(scripted):
            kind = scripted[attempt - 1]
            if kind == "timeout":
                raise TimeoutError(f"{self.name}: simulated timeout at {key!r}")
            if kind == "exc":
                raise RuntimeError(f"{self.name}: simulated transport error")
            if kind.startswith("http"):
                try:
                    status = int(kind[4:])
                except ValueError:
                    status = 500
                return Response(status_code=status, headers=dict(self.headers))
        body = (self.pages or {}).get(key)
        if body is None:
            return Response(status_code=404, headers=dict(self.headers))
        return Response(status_code=200, body=body, headers=dict(self.headers))


class HttpSource(Source):
    """真实 HTTP 来源（可注入 httpx.Client；离线测试不会用到）。"""

    def __init__(self, name, url, *, client=None, params=None, **kwargs):
        super().__init__(name=name, **kwargs)
        self.url = url
        self.client = client
        self.params = dict(params or {})

    def fetch(self, cursor, page, attempt, timeout):
        client = self.client
        if client is None:  # pragma: no cover - 需要 httpx
            import httpx

            client = httpx.Client()
        params = dict(self.params)
        if cursor is not None:
            params["cursor"] = cursor
        params["page"] = page
        try:
            resp = client.get(self.url, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 统一归为传输失败
            return Response(error=_error_kind(exc))
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - 非 JSON 视为无效响应
            body = None
        return Response(
            status_code=resp.status_code,
            body=body,
            headers=dict(resp.headers),
        )


# ---------------------------------------------------------------------------
# 有界缓存（并发安全 + TTL + LRU）
# ---------------------------------------------------------------------------

class PageCache:
    """线程安全、容量受限、带 TTL 的页面缓存。

    ``max_entries`` 是硬上限：并发采集时缓存条目数不会超过它，淘汰最久
    未使用项。``ttl_seconds`` 为 ``None`` 表示不主动过期。
    """

    def __init__(self, max_entries: int = 128, ttl_seconds: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = int(max_entries)
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._data: "dict[str, tuple[float, Any]]" = {}
        self.hits = 0
        self.misses = 0
        self.expired = 0
        self.evictions = 0

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, value = entry
            if self.ttl_seconds is not None and self._clock() - stored_at > self.ttl_seconds:
                del self._data[key]
                self.expired += 1
                self.misses += 1
                return None
            # 触碰为最近使用（dict 保序，先删后插把键移到末尾）
            del self._data[key]
            self._data[key] = (stored_at, value)
            self.hits += 1
            return value

    def put(self, key, value):
        with self._lock:
            if key in self._data:
                stored_at = self._data.pop(key)[0]
                self._data[key] = (stored_at, value)
                return
            if len(self._data) >= self.max_entries:
                oldest = next(iter(self._data))
                del self._data[oldest]
                self.evictions += 1
            self._data[key] = (self._clock(), value)

    def clear(self):
        with self._lock:
            self._data.clear()

    def delete(self, key):
        """移除单个键（用于作废异常页，避免缓存把坏响应带进重启）。"""
        with self._lock:
            self._data.pop(key, None)

    def __len__(self):
        with self._lock:
            return len(self._data)

    def stats(self):
        with self._lock:
            return {
                "size": len(self._data),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "expired": self.expired,
                "evictions": self.evictions,
            }


class RateLimiter:
    """每来源的最小请求间隔限速器（可注入 clock / sleep 便于离线验证）。"""

    def __init__(self, rate_per_sec: float, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 0.0 if not rate_per_sec else 1.0 / float(rate_per_sec)
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None
        self._lock = threading.Lock()
        self.waited = 0.0
        self.calls = 0

    def wait(self):
        if self.interval <= 0:
            with self._lock:
                self.calls += 1
            return 0.0
        with self._lock:
            now = self._clock()
            delay = 0.0
            if self._last is not None:
                delay = self.interval - (now - self._last)
            if delay > 0:
                self._sleep(delay)
                self.waited += delay
                now = self._clock()
            self._last = now
            self.calls += 1
            return max(0.0, delay)


# ---------------------------------------------------------------------------
# 解析辅助：别名读取、指纹、分页证据
# ---------------------------------------------------------------------------

def _field(doc, aliases, default=None):
    if not isinstance(doc, Mapping):
        return default
    for key in aliases:
        if key in doc and doc[key] is not None:
            return doc[key]
    return default


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _next_boundary(value):
    if value is None or value is False or value == "":
        return None
    return value if isinstance(value, str) else str(value)


def _identity(item):
    """用于页指纹的稳定身份：优先 DOI，其次 id，最后内容摘要。"""
    if isinstance(item, Mapping):
        doi = _field(item, DOI_KEYS)
        if doi:
            return "doi:" + str(doi).strip().lower()
        rid = _field(item, ID_KEYS)
        if rid:
            return "id:" + str(rid)
    return "sha:" + hashlib.sha256(
        json.dumps(item, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def page_fingerprint(items: Iterable[Any]) -> str:
    """内容指纹：只取身份集合，字段漂移不会改变同一页的指纹。"""
    ids = sorted(_identity(item) for item in items)
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:32]


def request_fingerprint(source: str, cursor, page) -> str:
    raw = f"{source}\x00{cursor if cursor is not None else ''}\x00{page}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def more_expected(*, next_cursor, total, total_pages, page, seen) -> bool:
    """分页证据：是否"还有更多数据"。空页据此区分结束与异常。"""
    if _next_boundary(next_cursor) is not None:
        return True
    total_i = _as_int(total)
    if total_i is not None and seen < total_i:
        return True
    pages_i = _as_int(total_pages)
    page_i = _as_int(page)
    if pages_i is not None and page_i is not None and page_i < pages_i:
        return True
    return False


def _response_version(body, headers) -> Optional[str]:
    found = _field(body, VERSION_KEYS)
    if found:
        return str(found)
    for key in ("x-response-version", "etag", "ETag", "last-modified"):
        if isinstance(headers, Mapping) and headers.get(key):
            return str(headers[key])
    return None


def parse_page(response: Response, page):
    """把响应解析成结构化页信息（容忍字段漂移）。"""
    body = response.body if isinstance(response.body, Mapping) else {}
    items = _field(body, ITEM_KEYS, [])
    if not isinstance(items, list):
        items = []
    return {
        "items": items,
        "next_cursor": _next_boundary(_field(body, NEXT_KEYS)),
        "total": _field(body, TOTAL_KEYS),
        "total_pages": _field(body, TOTAL_PAGES_KEYS),
        "page": _field(body, PAGE_KEYS, page),
        "response_version": _response_version(body, response.headers),
    }


# ---------------------------------------------------------------------------
# 记录补充：把"来源 / 请求边界 / 响应版本"接到记录上
# ---------------------------------------------------------------------------

def enrich(record, ctx):
    """把来源与分页来源信息接到记录上。

    全部字段都是确定性的（无时间戳），因此同一批次重放时记录内容不变，
    存储层可以判定 ``unchanged`` 并零写入。
    """
    rec = dict(record)
    rec["_stage_harvest"] = "ok"
    rec["_source"] = ctx.get("source")
    rec["_page"] = ctx.get("page")
    rec["_request_cursor"] = ctx.get("cursor")
    rec["_next_cursor"] = ctx.get("next_cursor")
    rec["_request_fp"] = ctx.get("request_fp")
    rec["_response_version"] = ctx.get("response_version")
    return rec


def process(record):
    """旧采集输入（无分页）仍可走记录级阶段：只补一个确定性标记。"""
    rec = dict(record)
    rec.setdefault("_stage_harvest", "ok")
    return rec


# ---------------------------------------------------------------------------
# 采集主循环
# ---------------------------------------------------------------------------

def _initial_state(source):
    return {
        "source": source.name,
        "cursor": source.start_cursor,
        "page": 1,
        "status": SourceStatus.PENDING.value,
        "request_fp": None,
        "response_version": None,
        "items_seen": 0,
        "last_fingerprint": None,
    }


def _retryable_status(status: int) -> bool:
    return status in (429, 408) or status >= 500


def harvest_source(
    con,
    source,
    prepare,
    *,
    merge=None,
    cache=None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retry_backoff: float = 0.0,
):
    """采集单个来源直到结束 / 失败。返回该来源的摘要。

    ``prepare(items, ctx)`` 必须返回 ``list[Record]``（已过 doi_map /
    version / dedup / keyer）。本函数只做采集与恢复点控制。

    核心约束：**内存游标只在** :func:`store.commit_harvest_page` **返回
    ``committed=True`` 之后才更新。** 任何失败路径都返回上一次已确认的
    游标，因此重启不会越过未落库的批次。
    """
    state = _store.load_harvest_state(con, source.name) or _initial_state(source)
    limiter = RateLimiter(source.rate_per_sec, clock=clock, sleep=sleep)
    summary = {
        "source": source.name,
        "status": state.get("status") or SourceStatus.PENDING.value,
        "cursor": state.get("cursor"),
        "page": int(state.get("page") or 1),
        "items_seen": int(state.get("items_seen") or 0),
        "requests": 0,
        "retries": 0,
        "cache_hits": 0,
        "outcomes": [],
        "writes": {"inserted": 0, "updated": 0, "unchanged": 0},
        "records": 0,
        "resumed_from": state.get("cursor"),
    }

    if summary["status"] == SourceStatus.COMPLETE.value:
        return summary

    cursor = state.get("cursor")
    page_no = int(state.get("page") or 1)
    items_seen = int(state.get("items_seen") or 0)
    visited = {cursor}

    def _trace(**fields):
        _store.record_request(con, {
            "source": source.name,
            "cursor": cursor,
            "page": page_no,
            "request_fp": fields.pop("request_fp", None),
            "cache_hit": fields.pop("cache_hit", False),
            **fields,
        })

    def _fetch(bypass_cache=False):
        """取一页，传输层失败按 ``max_retries`` 重试。

        返回 ``{"response", "version", "attempt", "cache_hit"}``；失败时
        ``response.ok()`` 为假，调用方据此走"不推进游标"的失败路径。
        """
        cache_key = (source.name, cursor, page_no)
        if cache is not None and not bypass_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                summary["cache_hits"] += 1
                summary["requests"] += 1
                return {
                    "response": cached[0],
                    "version": cached[1],
                    "attempt": 0,
                    "cache_hit": True,
                }

        attempt = 0
        while True:
            attempt += 1
            summary["requests"] += 1
            limiter.wait()
            try:
                response = source.fetch(cursor, page_no, attempt, source.timeout)
            except Exception as exc:  # noqa: BLE001 - 传输失败统一处理
                response = Response(error=_error_kind(exc))

            status = int(response.status_code or 0)
            if response.ok():
                version = _response_version(response.body, response.headers)
                if cache is not None:
                    cache.put(cache_key, (response, version))
                return {
                    "response": response,
                    "version": version,
                    "attempt": attempt,
                    "cache_hit": False,
                }

            retryable = response.error is not None or _retryable_status(status)
            if retryable and attempt <= source.max_retries:
                summary["retries"] += 1
                _trace(
                    request_fp=request_fingerprint(source.name, cursor, page_no),
                    outcome="retry",
                    http_status=status or None,
                    response_version=None,
                    attempt=attempt,
                )
                if retry_backoff > 0:
                    sleep(retry_backoff * (2 ** (attempt - 1)))
                continue
            return {
                "response": response,
                "version": None,
                "attempt": attempt,
                "cache_hit": False,
            }

    def _stop_partial(outcome, fetch, status=None):
        """失败收尾：只标记可继续状态，**不推进游标**。

        ``status='failed'`` 用于不可重试的请求失败（如 404），``partial``
        用于可重试但本次耗尽的失败；两者都保留重启可用的游标。
        """
        resp = (fetch or {}).get("response")
        http_status = int(resp.status_code or 0) if resp is not None else 0
        resolved = status or SourceStatus.PARTIAL.value
        summary["status"] = resolved
        summary["outcomes"].append(outcome)
        summary["cursor"] = cursor
        summary["page"] = page_no
        summary["items_seen"] = items_seen
        _store.save_harvest_state(con, {
            **state,
            "status": resolved,
            "cursor": cursor,
            "page": page_no,
            "items_seen": items_seen,
        })
        _trace(
            request_fp=request_fingerprint(source.name, cursor, page_no),
            outcome=outcome,
            http_status=http_status or None,
            response_version=(fetch or {}).get("version"),
            attempt=(fetch or {}).get("attempt"),
        )
        return summary

    def _failure_status(response):
        """4xx（除 408/429）不是暂时失败，但仍是"请求失败"而非"没有更多"。"""
        code = int(response.status_code or 0)
        if 400 <= code < 500 and code not in (408, 429):
            return SourceStatus.FAILED.value
        return SourceStatus.PARTIAL.value

    while True:
        req_fp = request_fingerprint(source.name, cursor, page_no)
        fetch = _fetch()
        response = fetch["response"]
        response_version = fetch["version"]
        attempt = fetch["attempt"]
        cache_hit = fetch["cache_hit"]

        # ---- 传输层 / HTTP 失败：不推进游标，标记可继续 ----
        if not response.ok():
            return _stop_partial(
                Outcome.FAILED.value, fetch, status=_failure_status(response)
            )

        parsed = parse_page(response, page_no)
        items = parsed["items"]
        next_cursor = parsed["next_cursor"]
        response_version = parsed["response_version"]

        # ---- 空页：用分页证据区分"确实结束"与"异常" ----
        if not items:
            anomaly_retries = 0
            while (
                more_expected(
                    next_cursor=next_cursor,
                    total=parsed["total"],
                    total_pages=parsed["total_pages"],
                    page=parsed["page"],
                    seen=items_seen,
                )
                and anomaly_retries < source.max_retries
            ):
                anomaly_retries += 1
                summary["retries"] += 1
                _trace(
                    request_fp=req_fp,
                    outcome="empty_anomaly_retry",
                    http_status=int(response.status_code or 0) or None,
                    response_version=response_version,
                    attempt=attempt,
                )
                if retry_backoff > 0:
                    sleep(retry_backoff)
                fetch = _fetch(bypass_cache=True)  # 异常页不进缓存，必须重取
                response = fetch["response"]
                response_version = fetch["version"]
                attempt = fetch["attempt"]
                cache_hit = fetch["cache_hit"]
                if not response.ok():
                    return _stop_partial(
                        Outcome.FAILED.value, fetch, status=_failure_status(response)
                    )
                parsed = parse_page(response, page_no)
                items = parsed["items"]
                next_cursor = parsed["next_cursor"]
                response_version = parsed["response_version"]

            if not items:
                if more_expected(
                    next_cursor=next_cursor,
                    total=parsed["total"],
                    total_pages=parsed["total_pages"],
                    page=parsed["page"],
                    seen=items_seen,
                ):
                    # 有证据说还有数据，但一直是空页 → 异常，不是结束。
                    # 作废该页缓存，避免重启时把坏响应当作已确认数据。
                    if cache is not None:
                        cache.delete((source.name, cursor, page_no))
                    return _stop_partial(Outcome.EMPTY_ANOMALY.value, fetch)

                # 有证据的结束：与记录同事务提交 status=complete。
                new_state = {
                    **state,
                    "status": SourceStatus.COMPLETE.value,
                    "cursor": next_cursor,
                    "page": page_no,
                    "request_fp": req_fp,
                    "response_version": response_version,
                    "items_seen": items_seen,
                    "last_fingerprint": page_fingerprint([]),
                }
                result = _store.commit_harvest_page(
                    con,
                    state=new_state,
                    records=[],
                    page_fingerprint=page_fingerprint([]),
                    trace={
                        "source": source.name,
                        "cursor": cursor,
                        "page": page_no,
                        "request_fp": req_fp,
                        "outcome": Outcome.EMPTY_END.value,
                        "http_status": int(response.status_code or 0) or None,
                        "response_version": response_version,
                        "attempt": attempt,
                        "cache_hit": cache_hit,
                        "record_keys": [],
                    },
                    merge=merge,
                )
                if not result["committed"]:
                    return _stop_partial(Outcome.COMMIT_FAILED.value, fetch)
                summary["status"] = SourceStatus.COMPLETE.value
                summary["outcomes"].append(Outcome.EMPTY_END.value)
                summary["cursor"] = new_state["cursor"]
                summary["page"] = page_no
                summary["items_seen"] = items_seen
                summary["writes"] = result["writes"]
                return summary

        # ---- 正常页：先准备记录，再原子提交，最后才推进游标 ----
        fingerprint = page_fingerprint(items)
        duplicate = _store.seen_page(con, source.name, fingerprint)
        ctx = {
            "source": source.name,
            "page": page_no,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "request_fp": req_fp,
            "response_version": response_version,
        }
        if duplicate:
            # 重复页：内容已提交过，不重复写入，只推进游标/边界。
            records = []
            record_keys = []
            page_items_seen = items_seen
        else:
            records = prepare(items, ctx) or []
            record_keys = [r.key for r in records]
            page_items_seen = items_seen + len(items)

        advancing = next_cursor is not None

        # 分页环路保护：next 指回已访问的游标或自身时不无限重取。
        # 本页数据仍然有效 → 照常提交，但游标不推进，状态如实标 partial。
        looped = advancing and (next_cursor == cursor or next_cursor in visited)
        if looped:
            advancing = False

        # 证据说还有数据、但没有可用的 next 边界 → 覆盖不完整，不能报完整。
        truncated = (not advancing) and more_expected(
            next_cursor=next_cursor,
            total=parsed["total"],
            total_pages=parsed["total_pages"],
            page=parsed["page"],
            seen=page_items_seen,
        )

        new_state = {
            **state,
            "status": (
                SourceStatus.PARTIAL.value
                if (looped or truncated)
                else (
                    SourceStatus.PENDING.value
                    if advancing
                    else SourceStatus.COMPLETE.value
                )
            ),
            "cursor": cursor if (looped or truncated) else (
                next_cursor if advancing else cursor
            ),
            "page": page_no + (1 if advancing else 0),
            "request_fp": req_fp,
            "response_version": response_version,
            "items_seen": page_items_seen,
            "last_fingerprint": fingerprint,
        }
        result = _store.commit_harvest_page(
            con,
            state=new_state,
            records=records,
            page_fingerprint=fingerprint,
            trace={
                "source": source.name,
                "cursor": cursor,
                "page": page_no,
                "request_fp": req_fp,
                "outcome": (
                    Outcome.PAGINATION_LOOP.value
                    if looped
                    else (
                        Outcome.TRUNCATED.value
                        if truncated
                        else (
                            Outcome.DUPLICATE.value if duplicate else Outcome.OK.value
                        )
                    )
                ),
                "http_status": int(response.status_code or 0) or None,
                "response_version": response_version,
                "attempt": attempt,
                "cache_hit": cache_hit,
                "record_keys": record_keys,
            },
            merge=merge,
        )

        # 提交确认是本函数推进游标的唯一依据。
        if not result["committed"]:
            return _stop_partial(Outcome.COMMIT_FAILED.value, fetch)

        for k in summary["writes"]:
            summary["writes"][k] += result["writes"].get(k, 0)
        summary["records"] += len(records)
        if looped or truncated:
            summary["status"] = SourceStatus.PARTIAL.value
            summary["outcomes"].append(
                Outcome.PAGINATION_LOOP.value if looped else Outcome.TRUNCATED.value
            )
            summary["cursor"] = cursor
            summary["page"] = page_no
            summary["items_seen"] = new_state["items_seen"]
            return summary
        summary["outcomes"].append(
            Outcome.DUPLICATE.value if duplicate else Outcome.OK.value
        )
        summary["cursor"] = new_state["cursor"]
        summary["page"] = new_state["page"]
        summary["items_seen"] = new_state["items_seen"]

        if not advancing:
            summary["status"] = SourceStatus.COMPLETE.value
            return summary

        cursor = new_state["cursor"]
        page_no = new_state["page"]
        items_seen = new_state["items_seen"]
        visited.add(cursor)
        state = new_state


def harvest_sources(
    sources,
    prepare,
    *,
    con=None,
    db_path=None,
    merge=None,
    cache=None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retry_backoff: float = 0.0,
    max_workers: int = 1,
):
    """采集多个来源；单个来源失败不影响其他来源。

    并发模式下每个来源使用独立 SQLite 连接（SQLite 连接不跨线程共享），
    共享一个 **容量受限** 的 :class:`PageCache`。并发度被 ``max_workers``
    封顶，因此缓存与连接资源都有明确上限。
    """
    sources = list(sources)
    if not sources:
        return {"sources": {}, "fed": 0, "requests": 0, "writes": {}, "cache": None}

    cache = cache if cache is not None else PageCache()

    def run_one(source, conn):
        return harvest_source(
            conn,
            source,
            prepare,
            merge=merge,
            cache=cache,
            clock=clock,
            sleep=sleep,
            retry_backoff=retry_backoff,
        )

    summaries = {}
    external_con = con
    if external_con is not None:
        for source in sources:
            summaries[source.name] = run_one(source, external_con)
    elif max_workers <= 1 or len(sources) == 1:
        owned = _store.connect(db_path)
        try:
            for source in sources:
                summaries[source.name] = run_one(source, owned)
        finally:
            owned.close()
    else:  # pragma: no cover - 并发路径
        from concurrent.futures import ThreadPoolExecutor

        workers = max(1, min(int(max_workers), len(sources)))

        def worker(source):
            conn = _store.connect(db_path)
            try:
                return source.name, run_one(source, conn)
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, summary in pool.map(worker, sources):
                summaries[name] = summary

    total_writes = {"inserted": 0, "updated": 0, "unchanged": 0}
    total_records = 0
    total_requests = 0
    for summary in summaries.values():
        for k in total_writes:
            total_writes[k] += summary["writes"].get(k, 0)
        total_records += summary["records"]
        total_requests += summary["requests"]

    # 用独立连接统计总行数，避免与调用方连接的生命周期耦合。
    count_con = external_con if external_con is not None else _store.connect(db_path)
    try:
        rows = _store.count(count_con)
    finally:
        if external_con is None:
            count_con.close()

    return {
        "sources": summaries,
        "fed": total_records,
        "requests": total_requests,
        "writes": total_writes,
        "rows": rows,
        "cache": cache.stats(),
    }


# ---------------------------------------------------------------------------
# 离线场景加载（供 demo / 测试复用）
# ---------------------------------------------------------------------------

def source_from_spec(spec: Mapping) -> FixtureSource:
    return FixtureSource(
        name=spec["name"],
        pages=dict(spec.get("pages") or {}),
        errors=dict(spec.get("errors") or {}),
        headers=dict(spec.get("headers") or {}),
        start_cursor=spec.get("start_cursor"),
        rate_per_sec=float(spec.get("rate_per_sec", 5.0)),
        timeout=float(spec.get("timeout", 5.0)),
        max_retries=int(spec.get("max_retries", 2)),
    )


def load_sources(path) -> list:
    """从 JSON 读取离线多来源场景（每项一个来源）。"""
    with open(path, encoding="utf-8") as fh:
        specs = json.load(fh)
    return [source_from_spec(spec) for spec in specs]
