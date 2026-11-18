"""多源采集：游标 / 限速 / 缓存 / 部分失败恢复的离线验证。

每个测试都在临时 sqlite 上运行，互不干扰。所有时间相关逻辑通过注入
clock/sleep 或 MockClock 驱动，不做真实等待。
"""
import json
import threading

import pytest

from src.papertrail import harvest, pipeline, store, version


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeClock:
    """可推进的单调时钟；sleep 直接把时间往前拨。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def page(items=None, next_cursor=None, **meta):
    body = {"items": list(items or [])}
    if next_cursor is not None:
        body["next_cursor"] = next_cursor
    body.update(meta)
    return body


def src(name, pages, *, errors=None, retries=2, rate=1000, **kw):
    return harvest.FixtureSource(
        name=name,
        pages=pages,
        errors=errors or {},
        max_retries=retries,
        rate_per_sec=rate,
        **kw,
    )


def run(sources, db, **kw):
    return pipeline.run_sources(sources, db_path=db, **kw)


def state(db, source):
    con = store.connect(db)
    try:
        return store.load_harvest_state(con, source)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 提交确认：游标不得越过未落库的批次
# ---------------------------------------------------------------------------

def test_cursor_does_not_advance_when_commit_fails(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}])})

    def boom(*a, **k):
        return {
            "committed": False,
            "error": "simulated write failure",
            "writes": {"inserted": 0, "updated": 0, "unchanged": 0},
            "cursor": None,
        }

    monkeypatch.setattr(store, "commit_harvest_page", boom)
    result = run([s], db)["sources"]["s1"]

    assert result["status"] == "partial"
    assert result["outcomes"] == ["commit_failed"]
    assert result["cursor"] is None          # 原地不动
    assert result["writes"]["inserted"] == 0
    # 没有任何记录落库
    con = store.connect(db)
    assert store.count(con) == 0
    con.close()

    # 恢复提交后，从同一个起点重放，完整落库
    monkeypatch.undo()
    again = run([src("s1", s.pages)], db)["sources"]["s1"]
    assert again["status"] == "complete"
    assert again["writes"]["inserted"] == 2


def test_restart_resumes_from_committed_cursor(tmp_path):
    db = str(tmp_path / "t.db")
    # 第一页成功，第二页超时耗尽重试 → partial，游标停在 "n"
    s = src(
        "s1",
        {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}], None)},
        errors={"n": ["timeout", "timeout", "timeout"]},
        retries=1,
    )
    first = run([s], db)["sources"]["s1"]
    assert first["status"] == "partial"
    assert first["cursor"] == "n"
    assert first["outcomes"] == ["ok", "failed"]

    st = state(db, "s1")
    assert st["cursor"] == "n"
    assert st["status"] == "partial"

    # 重启：换一个能成功的源，从 "n" 继续，不重取首页
    healthy = src("s1", s.pages, retries=0)
    second = run([healthy], db)["sources"]["s1"]
    assert second["resumed_from"] == "n"
    assert second["status"] == "complete"
    assert second["outcomes"] == ["ok"]
    # 首页记录已在库，重放不重复写入
    assert second["writes"] == {"inserted": 1, "updated": 0, "unchanged": 0}

    con = store.connect(db)
    assert store.count(con) == 2
    con.close()


# ---------------------------------------------------------------------------
# 空页：结束 vs 异常，必须由分页证据区分
# ---------------------------------------------------------------------------

def test_empty_page_without_evidence_is_end(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], "n"), "n": page([], None, total=1)})
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "complete"
    assert r["outcomes"] == ["ok", "empty_end"]
    assert r["cursor"] is None


def test_empty_page_with_next_cursor_is_anomaly_not_end(tmp_path):
    db = str(tmp_path / "t.db")
    # 空页但带 next_cursor → 还有更多数据 → 异常，不能当结束
    s = src(
        "s1",
        {"": page([{"id": "a"}], "n"), "n": page([], "n2")},
        retries=1,
    )
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "partial"
    assert "empty_anomaly" in r["outcomes"]
    assert "empty_end" not in r["outcomes"]
    assert r["cursor"] == "n"  # 停在异常页，下次从这里重试


def test_empty_page_with_unmet_total_is_anomaly(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], "n"), "n": page([], None, total=5)},
            retries=0)
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "partial"
    assert r["outcomes"] == ["ok", "empty_anomaly"]


def test_empty_page_anomaly_retries_then_recovers(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}])},
            retries=1)

    # 第一次取 "n" 返回空页 + 有更多证据，第二次正常
    seq = iter([
        harvest.Response(200, {"items": [], "next_cursor": "n2"}),
        harvest.Response(200, {"items": [{"id": "b"}], "next_cursor": None}),
    ])
    s.fetch = lambda cursor, p, attempt, timeout: (
        harvest.Response(200, {"items": [{"id": "a"}], "next_cursor": "n"})
        if cursor is None and p == 1
        else next(seq)
    )
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "complete"
    assert r["retries"] == 1
    assert "empty_anomaly_retry" in [t["outcome"]
                                     for t in _trace(db, "s1")]


def _trace(db, source):
    con = store.connect(db)
    try:
        return store.load_request_trace(con, source)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 失败 ≠ 结束；单源失败不影响其他源
# ---------------------------------------------------------------------------

def test_failure_is_not_reported_as_complete(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}])},
            errors={"": ["http500", "http500", "http500"]}, retries=1)
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "partial"
    assert r["status"] != "complete"
    assert r["outcomes"] == ["failed"]
    assert r["cursor"] is None


def test_partial_failure_does_not_block_other_sources(tmp_path):
    db = str(tmp_path / "t.db")
    bad = src("bad", {"": page([{"id": "x"}], None)},
              errors={"": ["timeout", "timeout", "timeout"]}, retries=1)
    good = src("good", {"": page([{"id": "y"}], None)})
    out = run([bad, good], db)
    assert out["sources"]["bad"]["status"] == "partial"
    assert out["sources"]["good"]["status"] == "complete"
    assert out["rows"] == 1  # 只有 good 的记录


# ---------------------------------------------------------------------------
# 重放：同一批次不重复写入
# ---------------------------------------------------------------------------

def test_replaying_a_committed_batch_writes_nothing(tmp_path):
    db = str(tmp_path / "t.db")
    pages = {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}], None)}
    first = run([src("s1", pages)], db)
    assert first["writes"]["inserted"] == 2

    # 状态已 complete：重放直接短路，零请求零写入
    replay = run([src("s1", pages)], db)
    assert replay["writes"] == {"inserted": 0, "updated": 0, "unchanged": 0}
    assert replay["sources"]["s1"]["requests"] == 0
    assert replay["rows"] == 2


def test_replaying_partial_batch_does_not_duplicate_rows(tmp_path):
    db = str(tmp_path / "t.db")
    pages = {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}], None)}
    failing = src("s1", pages, errors={"n": ["http500", "http500", "http500"]},
                  retries=1)
    run([failing], db)
    # 恢复正常后重跑；首页已提交，内容相同 → unchanged，不新增
    out = run([src("s1", pages)], db)["sources"]["s1"]
    assert out["status"] == "complete"
    assert out["writes"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    con = store.connect(db)
    assert store.count(con) == 2
    con.close()


# ---------------------------------------------------------------------------
# 重复页
# ---------------------------------------------------------------------------

def test_duplicate_page_is_detected_and_not_rewritten(tmp_path):
    db = str(tmp_path / "t.db")
    same = [{"id": "a"}, {"id": "b"}]  # 同一内容出现在两个游标下
    s = src("s1", {"": page(same, "n"), "n": page(same, None)})
    r = run([s], db)["sources"]["s1"]
    assert r["outcomes"] == ["ok", "duplicate_page"]
    # 重复页完全跳过写入
    assert r["writes"] == {"inserted": 2, "updated": 0, "unchanged": 0}
    con = store.connect(db)
    assert store.count(con) == 2
    con.close()


def test_duplicate_within_page_is_deduped_before_write(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}, {"id": "a"}], None)})
    r = run([s], db)["sources"]["s1"]
    assert r["records"] == 1
    assert r["writes"]["inserted"] == 1


# ---------------------------------------------------------------------------
# 字段漂移
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["items", "results", "data", "records", "entries"])
def test_field_drift_in_item_list(tmp_path, shape):
    db = str(tmp_path / f"{shape}.db")
    body = {shape: [{"id": "a"}]}
    s = src("s1", {"": body})
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "complete"
    assert r["records"] == 1


@pytest.mark.parametrize("key", ["next_cursor", "next", "nextPageToken"])
def test_field_drift_in_pagination(tmp_path, key):
    db = str(tmp_path / f"{key}.db")
    s = src("s1", {
        "": {"items": [{"id": "a"}], key: "n"},
        "n": {"items": [{"id": "b"}], key: None},
    })
    r = run([s], db)["sources"]["s1"]
    assert r["records"] == 2


@pytest.mark.parametrize("doi_field", ["doi", "DOI", "Doi"])
def test_doi_field_drift_collapses_to_one_key(tmp_path, doi_field):
    db = str(tmp_path / "t.db")
    first = {"items": [{doi_field: "10.1000/ABC", "id": "x"}], "next_cursor": "n"}
    second = {"items": [{"DOI": "https://doi.org/10.1000/abc", "id": "y"}]}
    s = src("s1", {"": first, "n": second})
    r = run([s], db)["sources"]["s1"]
    con = store.connect(db)
    assert store.keys(con) == ["doi:10.1000/abc"]
    con.close()
    assert r["writes"]["inserted"] == 1


def test_response_version_recorded_per_page(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": {"items": [{"id": "a"}], "schema_version": "2.0"}})
    run([s], db)
    assert state(db, "s1")["response_version"] == "2.0"


# ---------------------------------------------------------------------------
# 版本状态：正式版不被预印本降级，且与提交同事务
# ---------------------------------------------------------------------------

def test_published_wins_over_preprint_across_sources(tmp_path):
    db = str(tmp_path / "t.db")
    preprint = src("arxiv", {"": page(
        [{"id": "p1", "doi": "10.5555/x", "type": "preprint"}], None)})
    published = src("crossref", {"": page(
        [{"DOI": "10.5555/X", "type": "journal-article"}], None)})
    run([preprint, published], db)
    con = store.connect(db)
    rec = store.get(con, "doi:10.5555/x")
    versions = store.load_version_state(con)
    con.close()
    assert rec["_version"] == "published"
    assert versions["doi:10.5555/x"]["rank"] == version.VERSION_RANK["published"]


def test_preprint_does_not_downgrade_published_on_replay(tmp_path):
    db = str(tmp_path / "t.db")
    published = src("crossref", {"": page(
        [{"DOI": "10.5555/X", "type": "journal-article"}], None)})
    run([published], db)
    # 之后预印本源给出同一 DOI 的旧版本 → 不得降级
    preprint = src("arxiv", {"": page(
        [{"id": "p1", "doi": "10.5555/x", "type": "preprint"}], None)})
    r = run([preprint], db)["sources"]["arxiv"]
    con = store.connect(db)
    rec = store.get(con, "doi:10.5555/x")
    con.close()
    assert rec["_version"] == "published"
    # 内容无变化 → 不计为写入（inserted/updated 均为 0）
    assert r["writes"]["inserted"] == 0
    assert r["writes"]["updated"] == 0


# ---------------------------------------------------------------------------
# 限速
# ---------------------------------------------------------------------------

def test_rate_limiter_spaces_requests(tmp_path):
    clock = FakeClock()
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}], None)},
            rate=2)  # 0.5s 间隔
    run([s], db, clock=clock, sleep=clock.sleep)
    # 两个请求之间应至少等待一个间隔
    assert clock.sleeps and clock.sleeps[0] == pytest.approx(0.5)
    assert clock.now >= 0.5


def test_rate_limiter_does_not_amplify_across_retries(tmp_path):
    clock = FakeClock()
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], None)},
            errors={"": ["http500", "http500"]}, retries=2, rate=4)
    r = run([s], db, clock=clock, sleep=clock.sleep)["sources"]["s1"]
    assert r["status"] == "complete"
    # 3 次尝试 = 2 个间隔
    assert len(clock.sleeps) == 2


# ---------------------------------------------------------------------------
# 缓存：有界 + TTL + 并发安全
# ---------------------------------------------------------------------------

def test_cache_never_exceeds_max_entries():
    cache = harvest.PageCache(max_entries=3)
    for i in range(50):
        cache.put(f"k{i}", i)
    assert len(cache) == 3
    assert cache.stats()["evictions"] == 47


def test_cache_evicts_least_recently_used():
    cache = harvest.PageCache(max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")      # a 变成最近使用
    cache.put("c", 3)   # 应淘汰 b
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_cache_expiry_uses_injected_clock():
    clock = FakeClock()
    cache = harvest.PageCache(max_entries=4, ttl_seconds=10, clock=clock)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    clock.now += 11
    assert cache.get("k") is None
    assert cache.stats()["expired"] == 1


def test_expired_cache_triggers_refetch(tmp_path):
    clock = FakeClock()
    db = str(tmp_path / "t.db")
    pages = {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}], None)}
    cache = harvest.PageCache(max_entries=8, ttl_seconds=5, clock=clock)
    # 首次采集，页面进缓存后立即过期（模拟长时间中断）
    s = src("s1", pages, errors={"n": ["http500", "http500", "http500"]},
            retries=1)
    run([s], db, cache=cache, clock=clock, sleep=clock.sleep)
    clock.now += 100
    # 恢复后应重新取页，而不是用过期缓存
    out = run([src("s1", pages)], db, cache=cache, clock=clock,
              sleep=clock.sleep)["sources"]["s1"]
    assert out["status"] == "complete"
    assert out["writes"]["inserted"] == 1


def test_cache_is_safe_under_concurrent_use():
    cache = harvest.PageCache(max_entries=16)
    errors = []

    def worker(base):
        try:
            for i in range(200):
                cache.put(f"k{(base + i) % 40}", i)
                cache.get(f"k{(base + i * 3) % 40}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache) <= 16


def test_sources_share_a_bounded_cache(tmp_path):
    db = str(tmp_path / "t.db")
    cache = harvest.PageCache(max_entries=2)
    sources = [
        src(f"s{i}", {"": page([{"id": f"x{i}"}], None)}) for i in range(6)
    ]
    out = run(sources, db, cache=cache, max_workers=4)
    assert out["cache"]["size"] <= 2
    assert out["cache"]["max_entries"] == 2


# ---------------------------------------------------------------------------
# 并发采集
# ---------------------------------------------------------------------------

def test_concurrent_sources_all_persist(tmp_path):
    db = str(tmp_path / "t.db")
    sources = [
        src(f"s{i}", {
            "": page([{"id": f"{i}-a"}], "n"),
            "n": page([{"id": f"{i}-b"}], None),
        })
        for i in range(6)
    ]
    out = run(sources, db, max_workers=4)
    assert out["rows"] == 12
    assert all(v["status"] == "complete" for v in out["sources"].values())
    con = store.connect(db)
    assert store.count(con) == 12
    con.close()


# ---------------------------------------------------------------------------
# 旧采集输入
# ---------------------------------------------------------------------------

def test_legacy_flat_input_still_works():
    out = pipeline.run()
    assert out["fed"] == 4
    assert out["rows"] >= 3  # r-003 重复被去重


def test_legacy_records_have_no_harvest_state_requirement():
    # 旧记录只走记录级 process()，不应强制分页字段
    rec = harvest.process({"id": "legacy-1"})
    assert rec["_stage_harvest"] == "ok"
    assert "id" in rec


# ---------------------------------------------------------------------------
# 请求轨迹：重启后可核对请求边界、论文键、写入次数
# ---------------------------------------------------------------------------

def test_request_trace_records_boundaries_keys_and_writes(tmp_path):
    db = str(tmp_path / "t.db")
    pages = {"": page([{"id": "a"}], "n"), "n": page([{"id": "b"}], None)}
    run([src("s1", pages, errors={"n": ["http500", "http500", "http500"]},
             retries=1)], db)
    run([src("s1", pages)], db)  # 重启续采

    trace = _trace(db, "s1")
    ok = [t for t in trace if t["outcome"] == "ok"]
    assert [t["cursor"] for t in ok] == [None, "n"]
    assert [t["page"] for t in ok] == [1, 2]
    assert ok[0]["record_keys"] == ["a"]
    assert ok[1]["record_keys"] == ["b"]
    assert ok[0]["writes"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert ok[1]["writes"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    failed = [t for t in trace if t["outcome"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["cursor"] == "n"
    assert failed[0]["record_keys"] == []


def test_timeout_retry_attempts_are_traced_in_order(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {"": page([{"id": "a"}], None)},
            errors={"": ["timeout", "http500"]}, retries=2)
    run([s], db)
    trace = _trace(db, "s1")
    outcomes = [(t["outcome"], t["attempt"]) for t in trace]
    assert outcomes == [("retry", 1), ("retry", 2), ("ok", 3)]
    # 超时没有 HTTP 状态，不能伪造成 200
    assert trace[0]["http_status"] is None
    assert trace[1]["http_status"] == 500


def test_pagination_loop_keeps_page_but_does_not_advance(tmp_path):
    db = str(tmp_path / "t.db")
    s = src("s1", {
        "": page([{"id": "a"}], "n"),
        "n": page([{"id": "b"}], "n"),  # next 指回自身
    })
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "partial"
    assert r["outcomes"] == ["ok", "pagination_loop"]
    assert r["cursor"] == "n"
    # 本页有效数据仍被保留
    con = store.connect(db)
    assert store.keys(con) == ["a", "b"]
    con.close()


# ---------------------------------------------------------------------------
# 直接变量级测试：游标推进只由提交确认驱动
# ---------------------------------------------------------------------------

def test_commit_confirmed_page_is_the_only_cursor_advance(tmp_path):
    """提交失败那次调用的游标必须与提交前一致。"""
    db = str(tmp_path / "t.db")
    con = store.connect(db)
    try:
        s = src("s1", {"": page([{"id": "a"}], "n")})
        calls = []
        real = store.commit_harvest_page

        def observe(*a, **k):
            result = real(*a, **k)
            calls.append((k["state"].get("cursor"), result["committed"]))
            return result

        store.commit_harvest_page = observe
        harvest.harvest_source(con, s, pipeline.prepare_items)
        store.commit_harvest_page = real

        assert calls == [("n", True)]
        assert store.load_harvest_state(con, "s1")["cursor"] == "n"
    finally:
        con.close()


def test_truncated_page_is_not_reported_complete(tmp_path):
    db = str(tmp_path / "t.db")
    # total 说还有 8 条，但没有 next 边界 → 覆盖不完整
    s = src("s1", {"": page([{"id": "a"}, {"id": "b"}], None, total=10)})
    r = run([s], db)["sources"]["s1"]
    assert r["status"] == "partial"
    assert r["outcomes"] == ["truncated"]
    # 已拿到的数据仍然保留
    con = store.connect(db)
    assert store.keys(con) == ["a", "b"]
    con.close()


def test_no_more_data_is_distinct_from_failure():
    # 纯函数级：证据判定
    assert harvest.more_expected(next_cursor=None, total=None,
                                 total_pages=None, page=1, seen=0) is False
    assert harvest.more_expected(next_cursor="n", total=None,
                                 total_pages=None, page=1, seen=0) is True
    assert harvest.more_expected(next_cursor=None, total=5,
                                 total_pages=None, page=1, seen=2) is True
    assert harvest.more_expected(next_cursor=None, total=5,
                                 total_pages=None, page=1, seen=5) is False
    assert harvest.more_expected(next_cursor=None, total=None,
                                 total_pages=3, page=1, seen=0) is True
    assert harvest.more_expected(next_cursor=None, total=None,
                                 total_pages=3, page=3, seen=0) is False
