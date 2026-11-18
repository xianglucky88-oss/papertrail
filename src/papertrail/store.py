"""单一职责：本地 sqlite3 持久化。

除了记录表，这里还保存"采集恢复点"：

* ``harvest_state``   —— 每个来源的游标、请求边界、响应版本和状态；
* ``harvest_pages``   —— 已提交页的内容指纹（用于识别重复页）；
* ``harvest_requests``—— 请求轨迹（含失败/重试/缓存命中）；
* ``record_versions`` —— 每个规范键已见过的最高版本状态。

``commit_harvest_page`` 把本页记录、游标、请求边界、响应版本、页指纹和
请求轨迹放进 **同一个事务**。只有该事务提交成功，采集器才允许推进游标：
这就是"提交确认"约束，也是重启时唯一的恢复点。
"""
import json
import sqlite3
import time
from datetime import datetime, timezone

from .config import DB_PATH

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS records("
    "  key TEXT PRIMARY KEY,"
    "  data TEXT NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS harvest_state("
    "  source TEXT PRIMARY KEY,"
    "  cursor TEXT,"
    "  page INTEGER NOT NULL DEFAULT 1,"
    "  status TEXT NOT NULL DEFAULT 'pending',"
    "  request_fp TEXT,"
    "  response_version TEXT,"
    "  items_seen INTEGER NOT NULL DEFAULT 0,"
    "  last_fingerprint TEXT,"
    "  updated_at TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS harvest_pages("
    "  source TEXT NOT NULL,"
    "  fingerprint TEXT NOT NULL,"
    "  cursor TEXT,"
    "  created_at TEXT,"
    "  PRIMARY KEY(source, fingerprint)"
    ")",
    "CREATE TABLE IF NOT EXISTS harvest_requests("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  source TEXT NOT NULL,"
    "  cursor TEXT,"
    "  page INTEGER,"
    "  request_fp TEXT,"
    "  outcome TEXT,"
    "  http_status INTEGER,"
    "  response_version TEXT,"
    "  attempt INTEGER,"
    "  cache_hit INTEGER NOT NULL DEFAULT 0,"
    "  record_keys TEXT,"
    "  writes TEXT,"
    "  created_at TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS record_versions("
    "  key TEXT PRIMARY KEY,"
    "  rank INTEGER NOT NULL DEFAULT 0,"
    "  version TEXT,"
    "  updated_at TEXT"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_harvest_requests_source"
    "  ON harvest_requests(source, id)",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _open(db_path):
    con = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    # 并发首连时切换 WAL 需要短暂的排他锁；被占用就退避重试。
    for attempt in range(50):
        try:
            con.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            time.sleep(0.02 * min(attempt + 1, 10))
    con.execute("PRAGMA synchronous=NORMAL")
    for statement in _SCHEMA:
        con.execute(statement)
    con.commit()
    return con


def connect(db_path=None):
    """打开数据库并保证 schema 存在。测试可传入临时路径。

    并发采集时多个来源各自持有连接；WAL + busy_timeout 让并发写入排队
    等待而不是立即报 "database is locked"。
    """
    for attempt in range(50):
        try:
            return _open(db_path)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if attempt == 49:
                raise
            time.sleep(0.02 * min(attempt + 1, 10))


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def get(con, key):
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _dump(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=False, default=str)


def upsert(con, record, merge=None):
    """幂等写入一条记录，返回 ``inserted`` / ``updated`` / ``unchanged``。

    ``merge`` 用于把"版本状态"纳入写入判断（例如正式版不能被预印本覆盖）。
    内容完全相同则不执行 UPDATE，因此同一批次重放的写入次数为 0。
    """
    row = con.execute("SELECT data FROM records WHERE key=?", (record.key,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO records(key, data) VALUES(?, ?)",
            (record.key, _dump(record.data)),
        )
        return "inserted"

    old = json.loads(row[0])
    new = record.data if merge is None else merge(old, record.data)
    if new == old:
        return "unchanged"
    con.execute("UPDATE records SET data=? WHERE key=?", (_dump(new), record.key))
    return "updated"


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def keys(con):
    return [k for (k,) in con.execute("SELECT key FROM records ORDER BY key")]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# ---------------------------------------------------------------------------
# version state
# ---------------------------------------------------------------------------

def _touch_version_state(con, key, data):
    rank = int(data.get("_version_rank") or 0)
    version = data.get("_version")
    if rank <= 0 and version in (None, ""):
        return
    con.execute(
        "INSERT INTO record_versions(key, rank, version, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "  rank=excluded.rank,"
        "  version=COALESCE(excluded.version, record_versions.version),"
        "  updated_at=excluded.updated_at "
        "WHERE excluded.rank >= record_versions.rank",
        (key, rank, version, _now()),
    )


def load_version_state(con):
    return {
        key: {"rank": rank, "version": version}
        for key, rank, version in con.execute(
            "SELECT key, rank, version FROM record_versions ORDER BY key"
        )
    }


# ---------------------------------------------------------------------------
# harvest state / request trace
# ---------------------------------------------------------------------------

_STATE_FIELDS = (
    "cursor",
    "page",
    "status",
    "request_fp",
    "response_version",
    "items_seen",
    "last_fingerprint",
)


def load_harvest_state(con, source):
    row = con.execute(
        "SELECT source, cursor, page, status, request_fp, response_version,"
        "       items_seen, last_fingerprint, updated_at "
        "FROM harvest_state WHERE source=?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return {
        "source": row[0],
        "cursor": row[1],
        "page": row[2],
        "status": row[3],
        "request_fp": row[4],
        "response_version": row[5],
        "items_seen": row[6],
        "last_fingerprint": row[7],
        "updated_at": row[8],
    }


def _write_state(con, state):
    con.execute(
        "INSERT INTO harvest_state("
        "  source, cursor, page, status, request_fp, response_version,"
        "  items_seen, last_fingerprint, updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "  cursor=excluded.cursor,"
        "  page=excluded.page,"
        "  status=excluded.status,"
        "  request_fp=excluded.request_fp,"
        "  response_version=excluded.response_version,"
        "  items_seen=excluded.items_seen,"
        "  last_fingerprint=excluded.last_fingerprint,"
        "  updated_at=excluded.updated_at",
        (
            state["source"],
            state.get("cursor"),
            int(state.get("page") or 1),
            state.get("status") or "pending",
            state.get("request_fp"),
            state.get("response_version"),
            int(state.get("items_seen") or 0),
            state.get("last_fingerprint"),
            _now(),
        ),
    )


def save_harvest_state(con, state):
    """单独提交状态（仅用于失败标记，绝不推进游标）。"""
    try:
        _write_state(con, state)
        con.commit()
        return True
    except Exception:
        con.rollback()
        return False


def _insert_trace(con, trace):
    con.execute(
        "INSERT INTO harvest_requests("
        "  source, cursor, page, request_fp, outcome, http_status,"
        "  response_version, attempt, cache_hit, record_keys, writes, created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trace.get("source"),
            trace.get("cursor"),
            trace.get("page"),
            trace.get("request_fp"),
            trace.get("outcome"),
            trace.get("http_status"),
            trace.get("response_version"),
            trace.get("attempt"),
            1 if trace.get("cache_hit") else 0,
            json.dumps(trace.get("record_keys") or [], ensure_ascii=False),
            json.dumps(trace.get("writes") or {}, ensure_ascii=False),
            _now(),
        ),
    )


def record_request(con, trace):
    """记录一次未提交页的请求轨迹（重试、超时、提交失败等）。

    轨迹本身单独提交，但**不会**修改 harvest_state 的游标，因此重启后
    仍然从最后一个已确认的恢复点继续。
    """
    try:
        _insert_trace(con, trace)
        con.commit()
        return True
    except Exception:
        con.rollback()
        return False


def load_request_trace(con, source=None):
    sql = (
        "SELECT source, cursor, page, request_fp, outcome, http_status,"
        "       response_version, attempt, cache_hit, record_keys, writes, created_at "
        "FROM harvest_requests"
    )
    params = ()
    if source is not None:
        sql += " WHERE source=?"
        params = (source,)
    sql += " ORDER BY id"
    result = []
    for row in con.execute(sql, params):
        result.append(
            {
                "source": row[0],
                "cursor": row[1],
                "page": row[2],
                "request_fp": row[3],
                "outcome": row[4],
                "http_status": row[5],
                "response_version": row[6],
                "attempt": row[7],
                "cache_hit": bool(row[8]),
                "record_keys": json.loads(row[9] or "[]"),
                "writes": json.loads(row[10] or "{}"),
                "created_at": row[11],
            }
        )
    return result


def seen_page(con, source, fingerprint):
    row = con.execute(
        "SELECT 1 FROM harvest_pages WHERE source=? AND fingerprint=?",
        (source, fingerprint),
    ).fetchone()
    return row is not None


def _insert_page(con, source, fingerprint, cursor):
    con.execute(
        "INSERT OR IGNORE INTO harvest_pages(source, fingerprint, cursor, created_at)"
        " VALUES(?,?,?,?)",
        (source, fingerprint, cursor, _now()),
    )


# ---------------------------------------------------------------------------
# the single recovery point
# ---------------------------------------------------------------------------

def commit_harvest_page(
    con,
    *,
    state,
    records,
    page_fingerprint=None,
    trace=None,
    merge=None,
):
    """原子提交：记录 + 版本状态 + 游标/请求边界 + 页指纹 + 请求轨迹。

    返回 ``{"committed": bool, "writes": {...}, "cursor": ...}``。
    调用方 **必须** 检查 ``committed`` 后才可推进内存中的游标。
    任何异常都会回滚，游标保持上一个已确认的值（不会越过未落库的批次）。
    """
    writes = {"inserted": 0, "updated": 0, "unchanged": 0}
    try:
        for record in records:
            kind = upsert(con, record, merge=merge)
            writes[kind] += 1
            _touch_version_state(con, record.key, record.data if isinstance(record.data, dict) else {})
        if page_fingerprint:
            _insert_page(con, state["source"], page_fingerprint, state.get("cursor"))
        if state:
            _write_state(con, state)
        if trace:
            trace = dict(trace)
            trace.setdefault("writes", writes)
            _insert_trace(con, trace)
        con.commit()
        return {
            "committed": True,
            "writes": writes,
            "cursor": state.get("cursor") if state else None,
        }
    except Exception as exc:  # noqa: BLE001 - 任何写入失败都必须回滚
        con.rollback()
        return {
            "committed": False,
            "error": repr(exc),
            "writes": {"inserted": 0, "updated": 0, "unchanged": 0},
            "cursor": None,
        }
