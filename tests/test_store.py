"""存储层：提交确认、事务原子性、版本状态、请求轨迹。"""
import sqlite3

import pytest

from src.papertrail import store
from src.papertrail.models import Record


def _db(tmp_path, name="t.db"):
    return str(tmp_path / name)


def test_record_upsert_reports_outcomes(tmp_path):
    con = store.connect(_db(tmp_path))
    try:
        assert store.upsert(con, Record("k", {"v": 1})) == "inserted"
        assert store.upsert(con, Record("k", {"v": 1})) == "unchanged"
        assert store.upsert(con, Record("k", {"v": 2})) == "updated"
        assert store.get(con, "k") == {"v": 2}
    finally:
        con.close()


def test_commit_harvest_page_is_atomic(tmp_path):
    """批次中途失败 → 整批回滚，游标与记录都不落库。"""
    con = store.connect(_db(tmp_path))
    try:
        # 让第二条记录写入时抛错（模拟磁盘/约束故障）
        real_upsert = store.upsert
        calls = {"n": 0}

        def flaky(c, record, merge=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("simulated failure")
            return real_upsert(c, record, merge=merge)

        store.upsert = flaky
        try:
            result = store.commit_harvest_page(
                con,
                state={"source": "s", "cursor": "next", "page": 2,
                       "status": "pending", "items_seen": 2},
                records=[Record("a", {"v": 1}), Record("b", {"v": 2})],
                page_fingerprint="fp1",
                trace={"source": "s", "cursor": None, "page": 1,
                       "outcome": "ok"},
            )
        finally:
            store.upsert = real_upsert

        assert result["committed"] is False
        assert result["error"]
        # 第一条也不能残留
        assert store.count(con) == 0
        assert store.load_harvest_state(con, "s") is None
        assert not store.seen_page(con, "s", "fp1")
        assert store.load_request_trace(con, "s") == []
    finally:
        con.close()


def test_commit_harvest_page_writes_state_records_and_trace_together(tmp_path):
    con = store.connect(_db(tmp_path))
    try:
        state = {"source": "s", "cursor": "c2", "page": 2, "status": "pending",
                 "items_seen": 1, "request_fp": "rf", "response_version": "v1"}
        result = store.commit_harvest_page(
            con,
            state=state,
            records=[Record("doi:10.1/x", {"_version": "published",
                                           "_version_rank": 3})],
            page_fingerprint="fp",
            trace={"source": "s", "cursor": None, "page": 1, "outcome": "ok",
                   "record_keys": ["doi:10.1/x"]},
        )
        assert result["committed"] is True
        assert result["writes"] == {"inserted": 1, "updated": 0, "unchanged": 0}

        assert store.load_harvest_state(con, "s")["cursor"] == "c2"
        assert store.seen_page(con, "s", "fp")
        assert store.load_version_state(con)["doi:10.1/x"]["rank"] == 3
        trace = store.load_request_trace(con, "s")
        assert trace[0]["record_keys"] == ["doi:10.1/x"]
        assert trace[0]["writes"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    finally:
        con.close()


def test_version_state_never_downgrades(tmp_path):
    con = store.connect(_db(tmp_path))
    try:
        store.commit_harvest_page(
            con,
            state={"source": "s", "cursor": None, "page": 1, "status": "pending"},
            records=[Record("k", {"_version": "published", "_version_rank": 3})],
        )
        store.commit_harvest_page(
            con,
            state={"source": "s", "cursor": None, "page": 1, "status": "pending"},
            records=[Record("k2", {})],
        )
        # 直接写一个更低秩的版本状态，应被拒绝
        con.execute(
            "INSERT INTO record_versions(key, rank, version, updated_at) "
            "VALUES('k', 1, 'preprint', 'now') "
            "ON CONFLICT(key) DO UPDATE SET rank=excluded.rank "
            "WHERE excluded.rank >= record_versions.rank"
        )
        con.commit()
        assert store.load_version_state(con)["k"]["rank"] == 3
    finally:
        con.close()


def test_record_request_persists_without_moving_cursor(tmp_path):
    con = store.connect(_db(tmp_path))
    try:
        store.commit_harvest_page(
            con,
            state={"source": "s", "cursor": "c2", "page": 2, "status": "pending"},
            records=[],
        )
        store.record_request(
            con,
            {"source": "s", "cursor": "c2", "page": 2, "outcome": "retry",
             "attempt": 1},
        )
        # 轨迹落了，状态没被改写
        assert store.load_harvest_state(con, "s")["cursor"] == "c2"
        assert len(store.load_request_trace(con, "s")) == 1
    finally:
        con.close()


def test_save_harvest_state_rolls_back_on_error(tmp_path, monkeypatch):
    con = store.connect(_db(tmp_path))
    try:
        def boom(*a, **k):
            raise sqlite3.OperationalError("nope")

        monkeypatch.setattr(store, "_write_state", boom)
        assert store.save_harvest_state(con, {"source": "s"}) is False
    finally:
        con.close()
