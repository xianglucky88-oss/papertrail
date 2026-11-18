"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取 / 前缀扫描。

表结构是通用键值（key TEXT PRIMARY KEY, data TEXT），不包含任何业务列：
作者身份、版本链、待人工审核等业务实体都以稳定键序列化后存放在这里。
"""
import json, sqlite3
from .config import DB_PATH
from .models import Record

def connect(path=None):
    con = sqlite3.connect(str(path) if path is not None else DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con

def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False, sort_keys=True)))

def put(con, key, data):
    upsert(con, Record(key=key, data=data))

def get(con, key, default=None):
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row is not None else default

def scan(con, prefix):
    """按键前缀读取全部条目，返回 [(key, data), ...]，按 key 排序保证确定性。"""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%")
    rows = con.execute(
        "SELECT key,data FROM records WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
        (escaped + "%",),
    ).fetchall()
    return [(k, json.loads(d)) for k, d in rows]

def delete(con, key):
    con.execute("DELETE FROM records WHERE key=?", (key,))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]
