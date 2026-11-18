"""单一职责：标识层 —— DOI / arXiv 标识的规范化、别名映射与身份主键。

本模块只解决“同一个学术工作的多种写法如何归并到稳定身份”这一件事，并把可追踪
证据（原始写法、来源、状态、冲突、批次、重放轨迹）全部落盘，供下游使用：

* harvest 增量游标  → :func:`canonical_key_of` 取游标语义稳定键；
* version 版本链    → :func:`version_chain_of` 取预印本↔正式版/旧 DOI 链；
* dedup 合并判断    → 记录上的 ``doi_key`` / ``doi_work_key`` / ``doi_same_work``。

设计决策（题目要求的明确回答）
------------------------------
1. 失效链接（dead link）**不影响身份确认**。DOI 身份由字符串规范形态决定，链接能否
   解析只是别名的一种状态（``dead``），不是标识本身的有效性。失效别名的去留有两种
   可解释、可重放的策略，由 ``dead_link_policy`` 选择，**绝不靠覆盖字段处理**：

   * ``"legacy"``（默认）：失效别名继续沿用旧键 —— 已绑定 ``doi_key`` 的别名保持
     绑定，仅把别名状态置为 ``dead``，历史键原样可解析；
   * ``"pending"``：失效别名进入待审映射 —— 新出现、尚未绑定的失效写法写入
     ``aliases`` 表但状态为 ``pending``（不绑定 ``doi_key``），记录被打上
     ``doi_review="dead_alias"``，待人工/后续抓取裁决。

2. 别名升级（旧 DOI → 新 DOI、arXiv v1 → v3、小写 → 大写）后**历史键必须保住**：
   别名表是 append-only 的，旧写法永远解析到首次确认时的 ``doi_key``；同时登记一条
   ``supersede`` 事件与 ``version_chain`` 边，把旧键链到新键。历史键不会被重写，重放
   任意历史快照都得到相同解释。

3. 同 DOI 元数据冲突可追踪：同一 ``doi_key`` 下不同来源给出不一致元数据时，不丢任何
   一方，冲突写入 ``events``（``conflict``）并在 ``identifiers.conflict`` 上计数，
   记录被标 ``doi_conflict=true``，原始证据保留在 ``doi_evidence`` 中。

4. 主键稳定：``doi_key`` 对 DOI 取 ``doi:<小写规范 DOI>``，对 arXiv 取
   ``arxiv:<基础 id，去版本号>``，错误/无法识别的写法取 ``invalid:<sha256 前缀>``。
   同一身份的任何写法永远得到同一个键，与到达顺序、大小写、URL 包装无关。

对外兼容
--------
``process(record)`` 仍是流水线阶段函数：复制记录、补阶段标记（``_stage_doi_map``），
旧记录没有标识字段时行为与朴素基线一致（``naive`` 标记，不产生新键）。默认使用
进程级持久化 mapper（``doi_alias.db``），测试可用 :class:`IdentifierMapper` 的内存
实例注入。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

#: 失效别名继续沿用旧键（只标 dead，不改变已确认身份）
DEAD_LEGACY = "legacy"
#: 失效别名进入待审映射（不绑定身份键，记录进入 review）
DEAD_PENDING = "pending"
DEAD_POLICIES = (DEAD_LEGACY, DEAD_PENDING)

#: 别名/标识状态
STATUS_ACTIVE = "active"    # 正常可用
STATUS_DEAD = "dead"        # 链接/写法已失效，但身份仍可确认
STATUS_PENDING = "pending"  # 待人工/后续抓取裁决
STATUS_INVALID = "invalid"  # 写法本身不是合法标识（错误 DOI）

KIND_DOI = "doi"
KIND_ARXIV = "arxiv"
KIND_INVALID = "invalid"

# Crossref 与 arXiv 数据中常见的标识字段（异形字段名统一在这里消化）
# 注意：不放裸 "id" —— 那通常是记录自身主键（如 "r-001"），不是文献标识；
# 嵌套标识对象里的 id 由 _flatten_candidate 展开。
DOI_FIELDS = (
    "doi", "DOI", "Doi",
    "crossref_doi", "crossref_DOI",
    "url", "URL", "link", "resource",
    "identifier",
)
ARXIV_FIELDS = (
    "arxiv_id", "arXiv_id", "arxivId", "arxivid", "arXiv",
    "arxiv_url", "ePrint", "eprint",
    "url", "URL", "link", "identifier",
)
#: 元数据冲突比对字段（同身份不同来源给出的值在此集合上比较）
METADATA_FIELDS = ("title", "type", "container_title", "publisher", "published")

_DOI_PREFIX_RE = re.compile(r"^(?:doi|info:doi|dx\.doi\.org)[:/\s]*", re.IGNORECASE)
_ARXIV_PREFIX_RE = re.compile(r"^arxiv[:/\s]*", re.IGNORECASE)
# DOI 语法：10.<登记号>/<后缀>，登记号 1-9 位数字；后缀允许空白与常见标点，
# 仅排除控制字符及在 URL/XML 里会破坏结构的引号尖括号
_DOI_RE = re.compile(r"^10\.\d{1,9}/[^\x00-\x1f\"<>]+$")
# 旧版 arXiv： hep-th/9901001；新版： 1234.5678(v9)?
_ARXIV_OLD_RE = re.compile(
    r"^[a-z-]+(?:\.[A-Z]+)?/\d{7}$", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)
_TRAILING_JUNK_RE = re.compile(r"[.,);\]}>\"']+$")
_WS_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# 规范化纯函数（无副作用，可独立测试）
# ---------------------------------------------------------------------------


def _clean_text(raw: Any) -> str:
    """去首尾空白、折叠内部空白、去掉 ``doi:`` 之类的裸前缀与包裹括号。"""
    if raw is None:
        return ""
    text = _WS_RE.sub(" ", str(raw)).strip()
    if not text:
        return ""
    # 去掉常见包裹：( ... )、< ... >
    if len(text) >= 2 and text[0] in "(<['\"" and text[-1] in ")>]'\"":
        text = text[1:-1].strip()
    # 反复剥裸前缀，兼容 "doi: doi:10.x/y" 这类历史脏数据
    while True:
        nxt = _DOI_PREFIX_RE.sub("", text).strip()
        if nxt == text:
            break
        text = nxt
    return text


def _strip_url(text: str) -> tuple[str, str | None]:
    """把 URL 形态还原成路径部分；返回 ``(剩余字符串, 来源 host)``。

    兼容 doi.org / dx.doi.org / 各代理 host（含 ``/doi/`` 路径段）以及
    ``arxiv.org/abs/``、``/pdf/`` 等历史写法。
    """
    if "://" not in text and not text.lower().startswith(("doi.org/", "dx.doi.org/", "arxiv.org/")):
        return text, None
    candidate = text if "://" in text else "https://" + text
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return text, None
    host = (parts.hostname or "").lower()
    path = unquote(parts.path or "")
    # 代理形态 https://proxy.example.edu/login?url=https://doi.org/10.x/y
    if "url=" in (parts.query or ""):
        for piece in parts.query.split("&"):
            if piece.lower().startswith("url="):
                inner = unquote(piece[4:])
                rest, _ = _strip_url(inner)
                return rest, host
    if path.startswith("/doi/"):
        path = path[5:]
    elif path.startswith("/abs/") or path.startswith("/pdf/") or path.startswith("/html/"):
        path = path[5:]
    if path.endswith(".pdf"):
        path = path[:-4]
    return path.strip("/"), host


@dataclass(frozen=True)
class NormResult:
    """单个标识写法的规范化结果（证据不丢失：原始写法始终保留）。"""

    raw: str
    normalized: str          # 比较用规范形态（DOI 小写；arXiv 去版本小写）
    canonical: str           # 展示用规范形态（DOI 小写、arXiv 去版本，无 URL 包装）
    kind: str                # doi / arxiv / invalid
    valid: bool
    base: str                # arXiv 基础 id（无版本号）；DOI 等于 normalized
    version: str | None = None  # arXiv 版本号，如 "v3"
    via_host: str | None = None  # URL 写法时记录的 host，用于来源追踪
    reason: str | None = None    # invalid / 特殊处理的解释

    @property
    def stable_key(self) -> str:
        """该写法对应的稳定身份主键（无效写法也有确定性键）。"""
        return stable_key(self.kind, self.normalized, self.raw)


def stable_key(kind: str, normalized: str, raw: str = "") -> str:
    """身份主键：同身份任意写法恒定；无效写法用原文哈希保证确定性且不碰撞。"""
    if kind == KIND_DOI:
        return f"doi:{normalized}"
    if kind == KIND_ARXIV:
        return f"arxiv:{normalized}"
    digest = hashlib.sha256((raw or normalized).encode("utf-8")).hexdigest()[:16]
    return f"invalid:{digest}"


def normalize_doi(raw: Any) -> NormResult:
    """规范化一个可能是 DOI 的写法，兼容 URL / 大小写 / 前缀 / 代理 / 历史格式。"""
    original = "" if raw is None else str(raw)
    text = _clean_text(raw)
    host = None
    if text:
        text, host = _strip_url(text)
        text = _clean_text(text)
    # 去掉尾部标点（引用文献里常见 "10.1/a)." 的情况）
    text = _TRAILING_JUNK_RE.sub("", text).strip()
    if not text:
        return NormResult(original, "", "", KIND_INVALID, False, "",
                          via_host=host, reason="empty")
    lower = text.lower()
    if _DOI_RE.match(lower) is None:
        return NormResult(original, lower, text, KIND_INVALID, False, lower,
                          via_host=host, reason="not a syntactically valid DOI")
    return NormResult(original, lower, lower, KIND_DOI, True, lower,
                      via_host=host)


def normalize_arxiv(raw: Any) -> NormResult:
    """规范化一个可能是 arXiv id 的写法，兼容 URL / 旧版 id / 版本号。"""
    original = "" if raw is None else str(raw)
    text = _WS_RE.sub(" ", str(raw if raw is not None else "")).strip()
    if len(text) >= 2 and text[0] in "(<['\"" and text[-1] in ")>]'\"":
        text = text[1:-1].strip()
    while True:
        nxt = _ARXIV_PREFIX_RE.sub("", text).strip()
        if nxt == text:
            break
        text = nxt
    host = None
    if text:
        text, host = _strip_url(text)
        text = _WS_RE.sub(" ", text).strip()
    text = _TRAILING_JUNK_RE.sub("", text).strip()
    if not text:
        return NormResult(original, "", "", KIND_INVALID, False, "",
                          via_host=host, reason="empty")
    lower = text.lower()
    version = None
    m_ver = _ARXIV_VERSION_RE.search(lower)
    if m_ver:
        version = m_ver.group(0)
    base = _ARXIV_VERSION_RE.sub("", lower)
    if not (_ARXIV_NEW_RE.match(base + (version or "")) or _ARXIV_OLD_RE.match(base)):
        return NormResult(original, lower, text, KIND_INVALID, False, lower,
                          via_host=host, reason="not a syntactically valid arXiv id")
    return NormResult(original, base, base, KIND_ARXIV, True, base,
                      version=version, via_host=host)


def normalize_identifier(raw: Any, hint: str | None = None) -> NormResult:
    """按可选来源提示规范化未知写法；无提示时依次尝试 DOI / arXiv。"""
    text = "" if raw is None else str(raw)
    # 显式提示优先
    if hint == KIND_ARXIV:
        return normalize_arxiv(raw)
    if hint == KIND_DOI:
        return normalize_doi(raw)
    # 无提示：URL/字段里带 arxiv 字样先走 arXiv
    if "arxiv" in text.lower():
        arxiv_res = normalize_arxiv(raw)
        if arxiv_res.valid:
            return arxiv_res
    doi_res = normalize_doi(raw)
    if doi_res.valid:
        return doi_res
    arxiv_res = normalize_arxiv(raw)
    if arxiv_res.valid:
        return arxiv_res
    return doi_res


# ---------------------------------------------------------------------------
# Crossref / arXiv 异形字段提取
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def extract_identifiers(record: dict[str, Any]) -> list[NormResult]:
    """从 Crossref / arXiv 的异形字段中提取全部标识，保留每个写法的证据。

    优先级：显式 DOI 字段 → 显式 arXiv 字段 → 通用 URL/identifier 字段。
    同一原始写法可能被多个字段、按多种语法尝试；这里按原文去重并**优先保留合法
    解析**，只有所有尝试都失败时才保留一条 invalid 证据，避免通用字段（url 等）
    在已成功解析后又制造伪 invalid 标记。
    """
    candidates: dict[str, NormResult] = {}

    def _add(res: NormResult) -> None:
        if not res.raw:
            return
        old = candidates.get(res.raw)
        if old is None or (not old.valid and res.valid):
            candidates[res.raw] = res

    # 1) 显式 DOI 字段（Crossref 风格，含嵌套）
    for fld in DOI_FIELDS:
        if fld not in record:
            continue
        for item in _as_list(record.get(fld)):
            for candidate in _flatten_candidate(item):
                _add(normalize_doi(candidate))

    # 2) 显式 arXiv 字段（arXiv API 风格）
    for fld in ARXIV_FIELDS:
        if fld not in record:
            continue
        for item in _as_list(record.get(fld)):
            for candidate in _flatten_candidate(item):
                _add(normalize_arxiv(candidate))

    # 3) 纠偏：DOI 字段里混入 arXiv URL（或反之）时，按内容重新归类，
    #    避免跨源脏数据被永久判成 invalid
    for raw, res in list(candidates.items()):
        if not res.valid:
            retyped = normalize_identifier(raw)
            if retyped.valid:
                candidates[raw] = retyped

    return list(candidates.values())


def _flatten_candidate(item: Any) -> list[Any]:
    """展开嵌套字段形态：字符串、{"value": ...}、{"URL": ...}、arXiv dict 等。"""
    if item is None:
        return []
    if isinstance(item, str):
        return [item]
    if isinstance(item, dict):
        out: list[Any] = []
        for key in ("value", "URL", "url", "id", "ID", "$", "href", "name",
                    "primary_url", "primary-url"):
            if key in item and item[key]:
                out.append(item[key])
        # arXiv atom 风格 {"@term": "arxiv", "@value": "..."}
        if item.get("@term", "").lower() in ("arxiv", "doi"):
            out.append(item.get("@value"))
        return [v for v in out if v]
    return [item]


#: 每个冲突检测字段对应的 Crossref / arXiv 异形字段名
_METADATA_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "Title", "ti"),
    "type": ("type", "crossref_type"),
    "container_title": ("container_title", "container-title",
                        "journal_ref", "journal-ref"),
    "publisher": ("publisher",),
    "published": ("published", "published-print", "published_online",
                  "issued", "date"),
}


def extract_metadata(record: dict[str, Any]) -> dict[str, str]:
    """抽取用于冲突检测的元数据快照（Crossref/arXiv 异形字段统一）。"""
    snap: dict[str, str] = {}

    def _first(*names: str) -> str | None:
        for name in names:
            val = record.get(name)
            if isinstance(val, list) and val:
                val = val[0]
            if isinstance(val, dict):
                val = val.get("value") or val.get("title")
            if val not in (None, ""):
                return str(val).strip()
        return None

    for field_name in METADATA_FIELDS:
        value = _first(*_METADATA_ALIASES[field_name])
        if value:
            snap[field_name] = value
    return snap


# ---------------------------------------------------------------------------
# 持久化别名映射（sqlite，append-only，可重入、可恢复、可审计）
# ---------------------------------------------------------------------------


class IdentifierMapper:
    """标识映射器：规范化结果 → 稳定身份键，全程留痕。

    存储四类事实：
      * identifiers —— 身份主键及其代表规范值、冲突计数；
      * aliases     —— 每个原始写法（证据）如何解析，append-only；
      * events      —— 冲突、升级、失效、待审等审计事件；
      * batches     —— 批量处理/重试的进度（写入中断可恢复）。
    """

    def __init__(self, db_path: str | Path | None = None,
                 dead_link_policy: str = DEAD_LEGACY):
        if dead_link_policy not in DEAD_POLICIES:
            raise ValueError(f"unknown dead_link_policy: {dead_link_policy!r}")
        self.dead_link_policy = dead_link_policy
        if db_path is None or str(db_path) == ":memory:":
            self.con = sqlite3.connect(":memory:")
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.con.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS identifiers(
                key           TEXT PRIMARY KEY,   -- 稳定身份主键
                kind          TEXT NOT NULL,
                canonical     TEXT NOT NULL,      -- 代表规范值
                first_source  TEXT,
                conflict      INTEGER NOT NULL DEFAULT 0,
                created_seq   INTEGER NOT NULL,
                metadata_json TEXT
            );
            CREATE TABLE IF NOT EXISTS aliases(
                raw          TEXT NOT NULL,       -- 原始写法证据（永不改写）
                normalized   TEXT NOT NULL,
                kind         TEXT NOT NULL,
                key          TEXT,                -- 绑定的身份键；pending 时为 NULL
                status       TEXT NOT NULL,       -- active/dead/pending/invalid
                source       TEXT NOT NULL,
                via_host     TEXT,
                first_seen_seq INTEGER NOT NULL,
                PRIMARY KEY(raw, source)
            );
            CREATE TABLE IF NOT EXISTS events(
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id   TEXT,
                event_type TEXT NOT NULL,         -- register/conflict/dead/pending/supersede/link/invalid
                key        TEXT,
                raw        TEXT,
                source     TEXT,
                detail_json TEXT
            );
            CREATE TABLE IF NOT EXISTS batches(
                batch_id   TEXT PRIMARY KEY,
                attempts   INTEGER NOT NULL DEFAULT 0,
                done       INTEGER NOT NULL DEFAULT 0,
                total      INTEGER NOT NULL DEFAULT 0,
                status     TEXT NOT NULL,        -- running/done/failed
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS version_chain(
                from_key TEXT NOT NULL,
                to_key   TEXT NOT NULL,
                relation TEXT NOT NULL,          -- supersede/version/preprint_of
                source   TEXT NOT NULL,
                PRIMARY KEY(from_key, to_key, relation)
            );
            CREATE TABLE IF NOT EXISTS seq_counter(kind TEXT PRIMARY KEY, n INTEGER NOT NULL);
            """
        )
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # -- 内部工具 ---------------------------------------------------------

    def _next_seq(self, kind: str) -> int:
        cur = self.con.execute(
            "INSERT INTO seq_counter(kind,n) VALUES(?,1) "
            "ON CONFLICT(kind) DO UPDATE SET n=n+1 RETURNING n", (kind,))
        return int(cur.fetchone()[0])

    def _current_seq(self, kind: str) -> int:
        row = self.con.execute(
            "SELECT n FROM seq_counter WHERE kind=?", (kind,)).fetchone()
        return int(row[0]) if row else 0

    def _log(self, event_type: str, key: str | None, raw: str | None,
             source: str | None, detail: dict[str, Any] | None = None,
             batch_id: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO events(batch_id,event_type,key,raw,source,detail_json) "
            "VALUES(?,?,?,?,?,?)",
            (batch_id, event_type, key, raw, source,
             json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None))

    # -- 核心登记 ---------------------------------------------------------

    def register(self, norm: NormResult, source: str = "unknown",
                 dead: bool = False, batch_id: str | None = None,
                 metadata: dict[str, str] | None = None) -> str:
        """登记一个规范化写法，返回稳定身份键。纯幂等：重放结果不变。"""
        source = str(source or "unknown")
        cur = self.con.cursor()
        alias_row = cur.execute(
            "SELECT key,status FROM aliases WHERE raw=? AND source=?",
            (norm.raw, source)).fetchone()
        replay = alias_row is not None  # 同一来源的同一写法：重入，非新证据

        # 1) 错误 DOI / 非法写法：不参与身份归并，证据保留、可追踪
        if not norm.valid:
            key = norm.stable_key
            if cur.execute("SELECT 1 FROM identifiers WHERE key=?",
                           (key,)).fetchone() is None:
                cur.execute(
                    "INSERT INTO identifiers(key,kind,canonical,first_source,created_seq) "
                    "VALUES(?,?,?,?,?)",
                    (key, KIND_INVALID, norm.raw, source, self._next_seq("id")))
            if not replay:
                self._upsert_alias(norm, key, STATUS_INVALID, source, cur)
                self._log("invalid", key, norm.raw, source,
                          {"reason": norm.reason, "via_host": norm.via_host}, batch_id)
            self.con.commit()
            return key

        key = norm.stable_key

        # 2) 身份行（首次到达即定锚，之后永不改变主键/代表值）
        row = cur.execute("SELECT canonical FROM identifiers WHERE key=?", (key,)).fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO identifiers(key,kind,canonical,first_source,created_seq,metadata_json) "
                "VALUES(?,?,?,?,?,?)",
                (key, norm.kind, norm.canonical, source, self._next_seq("id"),
                 json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)))
            self._log("register", key, norm.raw, source,
                      {"canonical": norm.canonical, "via_host": norm.via_host}, batch_id)
        elif not replay:
            # 只有“新证据”才参与冲突检测；重放同一写法不产生新冲突事件
            self._detect_conflict(cur, key, row["canonical"], norm, source, metadata, batch_id)

        # 3) 别名行：append-only。已存在的别名永不被覆盖，只允许 dead 状态前移
        if alias_row is None:
            if dead and self.dead_link_policy == DEAD_PENDING:
                # 失效 + 待审策略：不绑定身份键，进入 pending 映射
                self._upsert_alias(norm, None, STATUS_PENDING, source, cur)
                self._log("pending", None, norm.raw, source,
                          {"reason": "dead link awaiting review", "would_be_key": key},
                          batch_id)
            else:
                status = STATUS_DEAD if dead else STATUS_ACTIVE
                self._upsert_alias(norm, key, status, source, cur)
                if dead:
                    self._log("dead", key, norm.raw, source,
                              {"note": "dead link does not change identity"}, batch_id)
        else:
            # 重入：同一原始写法再次出现。已绑定的键保持不动（保住历史键）；
            # 仅允许 active -> dead 的状态前移，不允许反向覆盖。
            if dead and alias_row["status"] == STATUS_ACTIVE:
                cur.execute("UPDATE aliases SET status=? WHERE raw=? AND source=?",
                            (STATUS_DEAD, norm.raw, source))
                self._log("dead", alias_row["key"], norm.raw, source,
                          {"note": "dead link does not change identity"}, batch_id)

        # 4) arXiv 版本号：登记版本链边（v1 -> v3 的历史键都保留）
        if norm.kind == KIND_ARXIV and norm.version:
            cur.execute(
                "INSERT OR IGNORE INTO version_chain(from_key,to_key,relation,source) "
                "VALUES(?,?,?,?)",
                (f"{key}:{norm.version.lower()}", key, "version", source))

        self.con.commit()
        return key

    def _upsert_alias(self, norm: NormResult, key: str | None,
                      status: str, source: str, cur: sqlite3.Cursor) -> None:
        # 仅在首次写入（新证据）时分配序号；重入不改变 first_seen_seq，保证可重放
        exists = cur.execute(
            "SELECT 1 FROM aliases WHERE raw=? AND source=?",
            (norm.raw, source)).fetchone()
        seq = self._current_seq("alias") if exists else self._next_seq("alias")
        cur.execute(
            "INSERT INTO aliases(raw,normalized,kind,key,status,source,via_host,first_seen_seq) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(raw,source) DO UPDATE SET "
            "status=CASE WHEN aliases.status='active' AND excluded.status='dead' "
            "THEN 'dead' ELSE aliases.status END",
            (norm.raw, norm.normalized, norm.kind, key, status, source,
             norm.via_host, seq))

    def _detect_conflict(self, cur: sqlite3.Cursor, key: str, canonical: str,
                         norm: NormResult, source: str,
                         metadata: dict[str, str] | None,
                         batch_id: str | None) -> None:
        """同 DOI 元数据冲突：双方证据都保留，冲突可追踪、不静默覆盖。"""
        conflicts: dict[str, dict[str, str]] = {}
        if metadata:
            row = cur.execute(
                "SELECT metadata_json FROM identifiers WHERE key=?", (key,)).fetchone()
            old_meta = json.loads(row["metadata_json"] or "{}")
            for field_name, new_val in metadata.items():
                old_val = old_meta.get(field_name)
                if old_val is not None and old_val != new_val:
                    conflicts[field_name] = {"stored": old_val, "incoming": new_val}
            # 合并不是覆盖：保留首个值，把新值放进事件证据；metadata_json 维持首锚
        if norm.canonical != canonical or conflicts:
            cur.execute("UPDATE identifiers SET conflict=conflict+1 WHERE key=?", (key,))
            self._log("conflict", key, norm.raw, source,
                      {"stored_canonical": canonical,
                       "incoming_canonical": norm.canonical,
                       "fields": conflicts}, batch_id)

    # -- 别名升级 / 版本链 -------------------------------------------------

    def supersede(self, old: NormResult | str, new: NormResult | str,
                  source: str = "unknown", relation: str = "supersede",
                  batch_id: str | None = None) -> tuple[str, str]:
        """登记“旧标识被新标识取代”。历史键保留，并加一条链边。

        返回 ``(old_key, new_key)``。旧写法的别名绑定**不被重写**，重放旧快照时
        仍解析到旧键，再沿 version_chain 走到新键。
        """
        old_norm = old if isinstance(old, NormResult) else normalize_identifier(old)
        new_norm = new if isinstance(new, NormResult) else normalize_identifier(new)
        old_key = self.register(old_norm, source=source, batch_id=batch_id)
        new_key = self.register(new_norm, source=source, batch_id=batch_id)
        if old_key != new_key:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO version_chain(from_key,to_key,relation,source) "
                "VALUES(?,?,?,?)", (old_key, new_key, relation, source))
            if cur.rowcount > 0:  # 只在首次建立升级关系时记事件，重放保持幂等
                self._log("supersede", new_key, old_norm.raw, source,
                          {"old_key": old_key, "new_key": new_key,
                           "relation": relation}, batch_id)
            self.con.commit()
        return old_key, new_key

    def link_works(self, key_a: str, key_b: str, relation: str,
                   source: str = "unknown") -> None:
        """登记跨身份关系（典型：arXiv 预印本 ↔ Crossref 正式 DOI）。"""
        self.con.execute(
            "INSERT OR IGNORE INTO version_chain(from_key,to_key,relation,source) "
            "VALUES(?,?,?,?)", (key_a, key_b, relation, source))
        self.con.commit()

    # -- 查询（供 harvest / version / dedup） ------------------------------

    def resolve(self, raw: str, source: str = "unknown") -> str | None:
        """按原始写法解析身份键；pending/invalid 或未知写法返回 None。"""
        row = self.con.execute(
            "SELECT key FROM aliases WHERE raw=? AND source=? AND key IS NOT NULL",
            (str(raw), source)).fetchone()
        if row:
            return row["key"]
        # 来源无关回退：同一写法在别的来源已确认身份
        row = self.con.execute(
            "SELECT key FROM aliases WHERE raw=? AND key IS NOT NULL LIMIT 1",
            (str(raw),)).fetchone()
        return row["key"] if row else None

    def canonical_key(self, key: str) -> str:
        """身份键的当前代表键：沿 supersede 链走到终点（链上历史键全部保留）。"""
        seen: set[str] = set()
        current = key
        while current not in seen:
            seen.add(current)
            row = self.con.execute(
                "SELECT to_key FROM version_chain WHERE from_key=? "
                "AND relation='supersede'", (current,)).fetchone()
            if row is None:
                return current
            current = row["to_key"]
        return current  # 防御性：理论上链无环

    def version_chain(self, key: str) -> list[dict[str, str]]:
        """返回该身份相关的版本/升级链边（version 阶段消费）。"""
        rows = self.con.execute(
            "SELECT from_key,to_key,relation,source FROM version_chain "
            "WHERE from_key=? OR to_key=? OR from_key LIKE ?",
            (key, key, key + ":%")).fetchall()
        return [dict(r) for r in rows]

    def same_work_keys(self, key: str) -> list[str]:
        """返回与该身份同属一个工作的全部键（dedup 合并判断消费）。

        沿 preprint_of / supersede 关系做双向传递闭包，因此 arXiv 记录先到、
        Crossref DOI 后到时，两边都能查到同一个合并组（与到达顺序无关）。
        """
        rows = self.con.execute(
            "SELECT from_key,to_key FROM version_chain "
            "WHERE relation IN ('preprint_of','supersede')").fetchall()
        adj: dict[str, set[str]] = {}
        for r in rows:
            adj.setdefault(r["from_key"], set()).add(r["to_key"])
            adj.setdefault(r["to_key"], set()).add(r["from_key"])
        out = {key}
        stack = [key]
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):  # 双向边
                if nxt not in out:
                    out.add(nxt)
                    stack.append(nxt)
        return sorted(out)

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type:
            rows = self.con.execute(
                "SELECT * FROM events WHERE event_type=? ORDER BY seq",
                (event_type,)).fetchall()
        else:
            rows = self.con.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def aliases_for(self, key: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT raw,normalized,kind,key,status,source,via_host "
            "FROM aliases WHERE key=? ORDER BY first_seen_seq", (key,)).fetchall()
        return [dict(r) for r in rows]

    def pending_aliases(self) -> list[dict[str, Any]]:
        """待审映射（pending 别名不绑定身份键，单独可查）。"""
        rows = self.con.execute(
            "SELECT raw,normalized,kind,key,status,source,via_host "
            "FROM aliases WHERE status='pending' ORDER BY first_seen_seq").fetchall()
        return [dict(r) for r in rows]

    def snapshot(self) -> dict[str, Any]:
        """完整映射状态快照（迁移/重入/中断恢复对比用）。"""
        def _table(name: str) -> list[dict[str, Any]]:
            return [dict(r) for r in self.con.execute(
                f"SELECT * FROM {name} ORDER BY 1,2,3").fetchall()]
        return {
            "identifiers": _table("identifiers"),
            "aliases": _table("aliases"),
            "version_chain": _table("version_chain"),
            "batches": _table("batches"),
            "events": [
                {k: v for k, v in dict(r).items() if k != "seq"}
                for r in self.con.execute(
                    "SELECT batch_id,event_type,key,raw,source,detail_json "
                    "FROM events ORDER BY seq").fetchall()
            ],
        }

    # -- 批量处理与重试（写入中断可恢复） -----------------------------------

    def begin_batch(self, batch_id: str, total: int) -> None:
        self.con.execute(
            "INSERT INTO batches(batch_id,attempts,done,total,status) VALUES(?,1,0,?, 'running') "
            "ON CONFLICT(batch_id) DO UPDATE SET attempts=attempts+1,status='running', "
            "last_error=NULL",
            (batch_id, total))
        self.con.commit()

    def mark_done(self, batch_id: str) -> None:
        self.con.execute(
            "UPDATE batches SET done=done+1 WHERE batch_id=?", (batch_id,))
        self.con.commit()

    def finish_batch(self, batch_id: str, status: str = "done",
                     error: str | None = None) -> None:
        self.con.execute(
            "UPDATE batches SET status=?, last_error=? WHERE batch_id=?",
            (status, error, batch_id))
        self.con.commit()

    def batch_state(self, batch_id: str) -> dict[str, Any] | None:
        row = self.con.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def run_batch(self, batch_id: str, items: Iterable[dict[str, Any]],
                  source: str = "batch") -> dict[str, Any]:
        """幂等批量登记：可安全重试；中断后重跑只补未完成项，已写别名不重复。

        每项形如 ``{"raw": ..., "dead": bool, "metadata": {...}, "supersedes": ...}``。
        返回批次终态。所有单项写入均已 commit，因此进程在任意时刻被打断，已完成的
        别名都已持久化，重跑时 :meth:`register` 的幂等性保证结果一致。
        """
        items = list(items)
        state = self.batch_state(batch_id)
        # 每次进入（首跑/重试/重入）都登记一次尝试；done 计数保留，作为续跑游标
        self.begin_batch(batch_id, len(items))
        # 已完成项跳过（重入/重试的核心）
        start = self.batch_state(batch_id)["done"]
        for idx in range(start, len(items)):
            item = items[idx]
            try:
                norm = normalize_identifier(item["raw"], item.get("hint"))
                self.register(norm, source=item.get("source", source),
                              dead=bool(item.get("dead", False)),
                              batch_id=batch_id,
                              metadata=item.get("metadata"))
                if item.get("supersedes"):
                    self.supersede(item["supersedes"], norm,
                                   source=item.get("source", source),
                                   batch_id=batch_id)
                self.mark_done(batch_id)
            except Exception as exc:  # 失败可追踪、可重试，不污染已完成项
                self.finish_batch(batch_id, status="failed", error=repr(exc))
                raise
        self.finish_batch(batch_id)
        return self.batch_state(batch_id)


# ---------------------------------------------------------------------------
# 进程级默认 mapper 与流水线阶段函数
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "doi_alias.db"
_default_mapper: IdentifierMapper | None = None


def get_mapper(db_path: str | Path | None = None,
               dead_link_policy: str = DEAD_LEGACY) -> IdentifierMapper:
    """获取进程级默认 mapper（流水线使用）；测试可用 :func:`reset_default_mapper` 重置。"""
    global _default_mapper
    if _default_mapper is None:
        _default_mapper = IdentifierMapper(
            db_path if db_path is not None else _DEFAULT_DB_PATH,
            dead_link_policy=dead_link_policy)
    return _default_mapper


def reset_default_mapper() -> None:
    """关闭并清空进程级 mapper（测试隔离用）。"""
    global _default_mapper
    if _default_mapper is not None:
        _default_mapper.close()
    _default_mapper = None


def canonical_key_of(record: dict[str, Any],
                     mapper: IdentifierMapper | None = None) -> str | None:
    """harvest 增量游标用：返回记录身份的当前代表键（沿升级链走到终点）。"""
    key = record.get("doi_key")
    if not key:
        return None
    m = mapper or get_mapper()
    return m.canonical_key(key)


def version_chain_of(record: dict[str, Any],
                     mapper: IdentifierMapper | None = None) -> list[dict[str, str]]:
    """version 版本链用：返回记录身份的版本/升级链边。"""
    key = record.get("doi_key")
    if not key:
        return []
    m = mapper or get_mapper()
    return m.version_chain(key)


def process(record: dict[str, Any], mapper: IdentifierMapper | None = None) -> dict[str, Any]:
    """流水线阶段：提取并登记标识，写入稳定身份键与可追踪证据。

    向后兼容：无任何标识字段的记录只补 ``_stage_doi_map="naive"``，与基线一致；
    有标识字段时标记为 ``ok`` 并附加 ``doi_*`` 字段。记录原始 ``id`` 不被修改，
    keyer 的稳定主键行为因此保持不变。
    """
    rec = dict(record)
    norms = extract_identifiers(rec)
    if not norms:
        rec.setdefault("_stage_doi_map", "naive")
        return rec

    m = mapper or get_mapper()
    source = str(rec.get("src") or rec.get("source") or "unknown")
    dead = bool(rec.get("dead_link") or rec.get("dead") or rec.get("link_dead"))
    metadata = extract_metadata(rec)

    evidence: list[dict[str, Any]] = []
    bound: list[tuple[NormResult, str]] = []  # (合法写法, 身份键)，保持对齐
    review_flags: list[str] = []
    conflict_seen = False

    for norm in norms:
        key = m.register(norm, source=source, dead=dead, metadata=metadata)
        evidence.append({
            "raw": norm.raw,
            "normalized": norm.normalized,
            "kind": norm.kind,
            "valid": norm.valid,
            "key": key,
            "via_host": norm.via_host,
            "version": norm.version,
            "reason": norm.reason,
        })
        if norm.valid:
            bound.append((norm, key))
        else:
            review_flags.append("invalid_identifier")
        # 待审策略下该写法处于 pending
        alias = m.con.execute(
            "SELECT status FROM aliases WHERE raw=? AND source=?",
            (norm.raw, source)).fetchone()
        if alias and alias["status"] == STATUS_PENDING:
            review_flags.append("dead_alias")
        id_row = m.con.execute(
            "SELECT conflict FROM identifiers WHERE key=?", (key,)).fetchone()
        if id_row and id_row["conflict"]:
            conflict_seen = True

    # 主身份：DOI 优先于 arXiv；同工作其余键供 dedup 判断
    doi_keys = [k for n, k in bound if n.kind == KIND_DOI]
    arxiv_keys = [k for n, k in bound if n.kind == KIND_ARXIV]
    if bound:
        primary = doi_keys[0] if doi_keys else arxiv_keys[0]
    else:
        # 全部写法都非法：错误 DOI 也有确定性键，进入待审而不是丢失记录
        primary = evidence[0]["key"]
        rec["doi_key"] = primary
        rec["doi_canonical"] = norms[0].raw
        rec["doi_work_key"] = primary
        rec["doi_same_work"] = [primary]
        rec["doi_evidence"] = evidence
        rec["doi_conflict"] = False
        rec["doi_review"] = sorted(set(review_flags) | {"invalid_identifier"})
        rec["_stage_doi_map"] = "review"
        return rec

    if arxiv_keys and doi_keys:
        m.link_works(arxiv_keys[0], doi_keys[0], "preprint_of", source)

    rec["doi_key"] = primary
    rec["doi_canonical"] = m.con.execute(
        "SELECT canonical FROM identifiers WHERE key=?", (primary,)).fetchone()["canonical"]
    rec["doi_work_key"] = m.canonical_key(primary)
    rec["doi_same_work"] = m.same_work_keys(primary)
    rec["doi_evidence"] = evidence
    rec["doi_conflict"] = conflict_seen
    rec["doi_review"] = sorted(set(review_flags))
    rec["_stage_doi_map"] = "ok"
    return rec
