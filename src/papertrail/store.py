"""单一职责：本地 sqlite3 持久化。

提供两层能力：
1. 通用 records 表的幂等 upsert / 计数 / 全量读取（流水线原始入口）；
2. version 子系统的版本归并表结构与面向查询的读取/写入助手。

本模块只负责存取，不含任何“是否同一篇/选哪个正式版”的业务判断，
业务规则全部位于 ``version.py``。版本子系统采用事件溯源：
``vx_events`` 只追加、不改写，其余 ``vx_*`` 表是可由事件重算的物化状态。
"""
import json
import sqlite3

from .config import DB_PATH

# --- 通用记录层（保持既有契约） -------------------------------------------

def connect(db_path=None):
    con = sqlite3.connect(str(db_path or DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    init_version_schema(con)
    return con


def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# --- version 子系统表结构 ---------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vx_events(
    event_id   TEXT PRIMARY KEY,
    paper_id   TEXT NOT NULL,
    seq_global INTEGER NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    ingest_ts  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vx_papers(
    paper_id       TEXT PRIMARY KEY,
    status         TEXT NOT NULL,              -- open / merged / retracted
    formal_node_id TEXT,
    formal_basis   TEXT,                       -- JSON：正式版选择依据
    created_ts     TEXT NOT NULL,
    updated_ts     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vx_paper_aliases(
    alias_paper_id TEXT PRIMARY KEY,           -- 旧 paper_id（可能是早期 arXiv/指纹锚点）
    paper_id       TEXT NOT NULL,              -- 当前规范 paper_id
    renamed_event  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vx_identifiers(
    id_type     TEXT NOT NULL,                 -- doi / arxiv / fingerprint
    id_value    TEXT NOT NULL,
    paper_id    TEXT NOT NULL,
    first_event TEXT NOT NULL,
    PRIMARY KEY (id_type, id_value)
);
CREATE TABLE IF NOT EXISTS vx_nodes(
    node_id    TEXT PRIMARY KEY,
    paper_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,                  -- preprint / published / retraction
    ord        INTEGER,                        -- 链内次序（可随重算变化）
    record_key TEXT NOT NULL UNIQUE,
    version_label TEXT,
    node_date  TEXT,
    metadata   TEXT NOT NULL,                  -- 该版完整元数据 JSON
    ingest_ts  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vx_edges(
    paper_id  TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node   TEXT NOT NULL,
    edge_kind TEXT NOT NULL,                   -- prev / supersedes / retracts
    PRIMARY KEY (paper_id, from_node, to_node, edge_kind)
);
CREATE TABLE IF NOT EXISTS vx_candidates(
    candidate_id TEXT PRIMARY KEY,
    paper_id     TEXT NOT NULL,
    other_paper  TEXT NOT NULL,
    reason       TEXT NOT NULL,                -- 规则名（见 version.CANDIDATE_RULES）
    evidence     TEXT NOT NULL,                -- JSON：保留的证据细节
    created_ts   TEXT NOT NULL,
    UNIQUE (paper_id, other_paper, reason)
);
CREATE TABLE IF NOT EXISTS vx_retractions(
    retraction_id  TEXT PRIMARY KEY,
    paper_id       TEXT NOT NULL,
    node_id        TEXT,                       -- 撤回通知自身节点（可能为空）
    target_node_id TEXT,                       -- 被撤回的版本节点（未知则空）
    record_key     TEXT NOT NULL,
    reason         TEXT,
    retracted_date TEXT,
    metadata       TEXT NOT NULL,
    ingest_ts      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vx_record_index(
    record_key TEXT PRIMARY KEY,
    paper_id   TEXT NOT NULL,
    node_id    TEXT,
    role       TEXT NOT NULL                   -- canonical / duplicate / retraction
);
"""


def init_version_schema(con):
    con.executescript(_SCHEMA)


def reset_version_state(con):
    """清空物化状态与事件流（仅供 recompute / 测试隔离使用）。"""
    for tbl in ("vx_record_index", "vx_retractions", "vx_candidates", "vx_edges",
                "vx_nodes", "vx_identifiers", "vx_paper_aliases", "vx_papers",
                "vx_events"):
        con.execute(f"DELETE FROM {tbl}")


# --- 事件流（只追加） -------------------------------------------------------

def next_event_seq(con):
    row = con.execute("SELECT COALESCE(MAX(seq_global), 0) + 1 FROM vx_events").fetchone()
    return row[0]


def append_event(con, event_id, paper_id, seq, kind, payload, ingest_ts):
    con.execute(
        "INSERT OR IGNORE INTO vx_events(event_id, paper_id, seq_global, kind, payload, ingest_ts) "
        "VALUES (?,?,?,?,?,?)",
        (event_id, paper_id, seq, kind, json.dumps(payload, ensure_ascii=False), ingest_ts),
    )
    return con.total_changes


def event_exists(con, event_id):
    return con.execute("SELECT 1 FROM vx_events WHERE event_id=?", (event_id,)).fetchone() is not None


def all_events(con):
    rows = con.execute(
        "SELECT event_id, paper_id, seq_global, kind, payload, ingest_ts "
        "FROM vx_events ORDER BY seq_global").fetchall()
    return [{"event_id": e, "paper_id": p, "seq": s, "kind": k,
             "payload": json.loads(pl), "ingest_ts": ts}
            for e, p, s, k, pl, ts in rows]


def events_for_paper(con, paper_id):
    rows = con.execute(
        "SELECT event_id, paper_id, seq_global, kind, payload, ingest_ts "
        "FROM vx_events WHERE paper_id=? ORDER BY seq_global", (paper_id,)).fetchall()
    return [{"event_id": e, "paper_id": p, "seq": s, "kind": k,
             "payload": json.loads(pl), "ingest_ts": ts}
            for e, p, s, k, pl, ts in rows]


# --- 论文身份 ---------------------------------------------------------------

def put_paper(con, paper_id, status, formal_node_id, formal_basis, created_ts, updated_ts):
    con.execute(
        "INSERT INTO vx_papers(paper_id, status, formal_node_id, formal_basis, created_ts, updated_ts) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET "
        "status=excluded.status, formal_node_id=excluded.formal_node_id, "
        "formal_basis=excluded.formal_basis, updated_ts=excluded.updated_ts",
        (paper_id, status, formal_node_id,
         json.dumps(formal_basis, ensure_ascii=False) if formal_basis is not None else None,
         created_ts, updated_ts),
    )


def get_paper(con, paper_id):
    row = con.execute(
        "SELECT paper_id, status, formal_node_id, formal_basis, created_ts, updated_ts "
        "FROM vx_papers WHERE paper_id=?", (paper_id,)).fetchone()
    if row is None:
        return None
    return {"paper_id": row[0], "status": row[1], "formal_node_id": row[2],
            "formal_basis": json.loads(row[3]) if row[3] else None,
            "created_ts": row[4], "updated_ts": row[5]}


def all_papers(con):
    return [get_paper(con, r[0])
            for r in con.execute("SELECT paper_id FROM vx_papers ORDER BY paper_id")]


def active_papers(con):
    """当前有效的论文（排除已并链/改名的 merged 行）。"""
    return [get_paper(con, r[0])
            for r in con.execute("SELECT paper_id FROM vx_papers "
                                 "WHERE status != 'merged' ORDER BY paper_id")]


def canonical_paper_id(con, paper_id):
    """沿别名链把任意历史 paper_id 解析为当前规范 id。"""
    seen = set()
    cur = paper_id
    while cur and cur not in seen:
        seen.add(cur)
        row = con.execute("SELECT paper_id FROM vx_paper_aliases WHERE alias_paper_id=?",
                          (cur,)).fetchone()
        if row is None:
            break
        cur = row[0]
    return cur


def add_alias(con, alias_paper_id, paper_id, renamed_event):
    con.execute(
        "INSERT OR IGNORE INTO vx_paper_aliases(alias_paper_id, paper_id, renamed_event) "
        "VALUES (?,?,?)", (alias_paper_id, paper_id, renamed_event))


def aliases_for_paper(con, paper_id):
    rows = con.execute("SELECT alias_paper_id, renamed_event FROM vx_paper_aliases "
                       "WHERE paper_id=? ORDER BY alias_paper_id", (paper_id,)).fetchall()
    return [{"alias_paper_id": a, "renamed_event": e} for a, e in rows]


def put_identifier(con, id_type, id_value, paper_id, first_event):
    con.execute(
        "INSERT OR IGNORE INTO vx_identifiers(id_type, id_value, paper_id, first_event) "
        "VALUES (?,?,?,?)", (id_type, id_value, paper_id, first_event))


def find_identifier(con, id_type, id_value):
    row = con.execute("SELECT paper_id, first_event FROM vx_identifiers "
                      "WHERE id_type=? AND id_value=?", (id_type, id_value)).fetchone()
    return None if row is None else {"paper_id": row[0], "first_event": row[1]}


def identifiers_for_paper(con, paper_id):
    rows = con.execute("SELECT id_type, id_value, first_event FROM vx_identifiers "
                       "WHERE paper_id=? ORDER BY id_type, id_value", (paper_id,)).fetchall()
    return [{"id_type": t, "id_value": v, "first_event": e} for t, v, e in rows]


def reassign_identifiers(con, old_paper, new_paper):
    con.execute("UPDATE OR IGNORE vx_identifiers SET paper_id=? WHERE paper_id=?",
                (new_paper, old_paper))
    # 旧论文上与新论文冲突的标识（同一 doi/arxiv 已归并）直接移除
    con.execute("DELETE FROM vx_identifiers WHERE paper_id=?", (old_paper,))


# --- 版本节点 / 边 ----------------------------------------------------------

def put_node(con, node):
    con.execute(
        "INSERT INTO vx_nodes(node_id, paper_id, kind, ord, record_key, version_label, "
        "node_date, metadata, ingest_ts) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET paper_id=excluded.paper_id, kind=excluded.kind, "
        "ord=excluded.ord, version_label=excluded.version_label, node_date=excluded.node_date, "
        "metadata=excluded.metadata, ingest_ts=excluded.ingest_ts",
        (node["node_id"], node["paper_id"], node["kind"], node.get("ord"),
         node["record_key"], node.get("version_label"), node.get("node_date"),
         json.dumps(node["metadata"], ensure_ascii=False), node["ingest_ts"]))


def get_node(con, node_id):
    row = con.execute(
        "SELECT node_id, paper_id, kind, ord, record_key, version_label, node_date, metadata, ingest_ts "
        "FROM vx_nodes WHERE node_id=?", (node_id,)).fetchone()
    if row is None:
        return None
    return {"node_id": row[0], "paper_id": row[1], "kind": row[2], "ord": row[3],
            "record_key": row[4], "version_label": row[5], "node_date": row[6],
            "metadata": json.loads(row[7]), "ingest_ts": row[8]}


def nodes_for_paper(con, paper_id):
    rows = con.execute(
        "SELECT node_id FROM vx_nodes WHERE paper_id=? ORDER BY COALESCE(ord, 0), node_id",
        (paper_id,)).fetchall()
    return [get_node(con, r[0]) for r in rows]


def find_node_by_record(con, record_key):
    row = con.execute("SELECT node_id FROM vx_nodes WHERE record_key=?", (record_key,)).fetchone()
    return get_node(con, row[0]) if row else None


def find_node_by_version(con, paper_id, kind, version_label):
    row = con.execute(
        "SELECT node_id FROM vx_nodes WHERE paper_id=? AND kind=? AND COALESCE(version_label,'')=?",
        (paper_id, kind, version_label or "")).fetchone()
    return get_node(con, row[0]) if row else None


def replace_edges(con, paper_id, edges):
    con.execute("DELETE FROM vx_edges WHERE paper_id=?", (paper_id,))
    con.executemany(
        "INSERT OR IGNORE INTO vx_edges(paper_id, from_node, to_node, edge_kind) VALUES (?,?,?,?)",
        [(paper_id, a, b, k) for a, b, k in edges])


def edges_for_paper(con, paper_id):
    rows = con.execute("SELECT from_node, to_node, edge_kind FROM vx_edges "
                       "WHERE paper_id=? ORDER BY from_node, to_node", (paper_id,)).fetchall()
    return [{"from_node": a, "to_node": b, "edge_kind": k} for a, b, k in rows]


# --- 候选（证据不足，暂不归并） ---------------------------------------------

def add_candidate(con, candidate_id, paper_id, other_paper, reason, evidence, created_ts):
    con.execute(
        "INSERT OR IGNORE INTO vx_candidates(candidate_id, paper_id, other_paper, reason, evidence, created_ts) "
        "VALUES (?,?,?,?,?,?)",
        (candidate_id, paper_id, other_paper, reason,
         json.dumps(evidence, ensure_ascii=False), created_ts))


def candidates_for_paper(con, paper_id):
    rows = con.execute(
        "SELECT candidate_id, paper_id, other_paper, reason, evidence, created_ts "
        "FROM vx_candidates WHERE paper_id=? OR other_paper=? ORDER BY created_ts, candidate_id",
        (paper_id, paper_id)).fetchall()
    return [{"candidate_id": c, "paper_id": p, "other_paper": o, "reason": r,
             "evidence": json.loads(e), "created_ts": t}
            for c, p, o, r, e, t in rows]


def all_candidates(con):
    rows = con.execute(
        "SELECT candidate_id, paper_id, other_paper, reason, evidence, created_ts "
        "FROM vx_candidates ORDER BY created_ts, candidate_id").fetchall()
    return [{"candidate_id": c, "paper_id": p, "other_paper": o, "reason": r,
             "evidence": json.loads(e), "created_ts": t}
            for c, p, o, r, e, t in rows]


def delete_candidate_pair(con, paper_id, other_paper):
    con.execute("DELETE FROM vx_candidates WHERE (paper_id=? AND other_paper=?) "
                "OR (paper_id=? AND other_paper=?)",
                (paper_id, other_paper, other_paper, paper_id))


# --- 撤回事实 ---------------------------------------------------------------

def add_retraction(con, ret):
    con.execute(
        "INSERT OR IGNORE INTO vx_retractions(retraction_id, paper_id, node_id, target_node_id, "
        "record_key, reason, retracted_date, metadata, ingest_ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (ret["retraction_id"], ret["paper_id"], ret.get("node_id"), ret.get("target_node_id"),
         ret["record_key"], ret.get("reason"), ret.get("retracted_date"),
         json.dumps(ret["metadata"], ensure_ascii=False), ret["ingest_ts"]))


def retractions_for_paper(con, paper_id):
    rows = con.execute(
        "SELECT retraction_id, paper_id, node_id, target_node_id, record_key, reason, "
        "retracted_date, metadata, ingest_ts FROM vx_retractions WHERE paper_id=? "
        "ORDER BY ingest_ts, retraction_id", (paper_id,)).fetchall()
    return [{"retraction_id": r, "paper_id": p, "node_id": n, "target_node_id": tn,
             "record_key": rk, "reason": rs, "retracted_date": d,
             "metadata": json.loads(m), "ingest_ts": ts}
            for r, p, n, tn, rk, rs, d, m, ts in rows]


# --- 记录索引（doi_map / dedup 阶段查询） ------------------------------------

def index_record(con, record_key, paper_id, node_id, role):
    con.execute(
        "INSERT INTO vx_record_index(record_key, paper_id, node_id, role) VALUES (?,?,?,?) "
        "ON CONFLICT(record_key) DO UPDATE SET paper_id=excluded.paper_id, "
        "node_id=excluded.node_id, role=excluded.role",
        (record_key, paper_id, node_id, role))


def lookup_record(con, record_key):
    row = con.execute("SELECT record_key, paper_id, node_id, role FROM vx_record_index "
                      "WHERE record_key=?", (record_key,)).fetchone()
    if row is None:
        return None
    return {"record_key": row[0], "paper_id": row[1], "node_id": row[2], "role": row[3]}


def reassign_records(con, old_paper, new_paper):
    con.execute("UPDATE vx_record_index SET paper_id=? WHERE paper_id=?", (new_paper, old_paper))


def reassign_nodes(con, old_paper, new_paper):
    con.execute("UPDATE vx_nodes SET paper_id=? WHERE paper_id=?", (new_paper, old_paper))


def reassign_retractions(con, old_paper, new_paper):
    con.execute("UPDATE vx_retractions SET paper_id=? WHERE paper_id=?", (new_paper, old_paper))


def mark_paper_status(con, paper_id, status):
    con.execute("UPDATE vx_papers SET status=? WHERE paper_id=?", (status, paper_id))
