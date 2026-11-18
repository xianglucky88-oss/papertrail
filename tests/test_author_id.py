"""作者消歧单元与场景测试：同名不同人、改名、缺标识+机构冲突、匿名、
重复导入稳定性、人工确认与中断恢复。"""
import inspect
import json
from pathlib import Path

import pytest

from src.papertrail import author_id, store, pipeline, normalizer
from src.papertrail.models import Context

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "papers.json"


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "test.db")
    yield c
    c.close()


def _load():
    return json.loads(FIX.read_text(encoding="utf-8"))


def _call(mod, rec, ctx):
    return mod.process(rec, ctx) if len(
        inspect.signature(mod.process).parameters) >= 2 else mod.process(rec)


def _run_paper(con, raw):
    ctx = Context(con=con)
    rec = raw
    for name in ["normalizer", "harvest", "author_id"]:
        mod = __import__("src.papertrail." + name, fromlist=["process"])
        rec = _call(mod, rec, ctx)
    return rec


def _ident_map(con):
    return {v["identity_id"]: v for _k, v in store.scan(con, author_id.IDENTITY_PREFIX)}


def test_orcid_validation():
    assert normalizer.normalize_orcid("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert normalizer.normalize_orcid("0000-0002-1825-0098") == ""   # 校验位错误
    assert normalizer.normalize_orcid("not-an-orcid") == ""


def test_homonym_different_people_keep_distinct_identities(con):
    raws = _load()
    # 冷启动顺序：无 ORCID 的海洋大学 Wei Zhang 先独立建身份
    r3 = _run_paper(con, raws[2])
    wz_ocean = r3["author_resolution"][0]["author_id"]
    assert wz_ocean.startswith(author_id.IDENTITY_PREFIX + "lid:")
    # 再导入 ORCID 版 Wei Zhang：同名不会被并到既有 lid 身份
    r1 = _run_paper(con, raws[0])
    wz_orcid = r1["author_resolution"][0]["author_id"]
    assert wz_orcid.startswith(author_id.IDENTITY_PREFIX + "orcid:")
    assert wz_orcid != wz_ocean                       # 同名不同人未被合并
    idents = _ident_map(con)
    assert idents[wz_ocean]["name"] == "Wei Zhang"
    assert "Ocean University" in idents[wz_ocean]["affiliations"]


def test_homonym_in_natural_order_is_guarded_not_auto_merged(con):
    raws = _load()
    _run_paper(con, raws[0])                          # 先有 ORCID 版 Wei Zhang
    r3 = _run_paper(con, raws[2])                     # 无 ORCID 同名、机构冲突
    res = r3["author_resolution"][0]
    assert res["status"] == "pending"
    assert res["author_id"] is None
    assert res["reason"] in ("affiliation_conflict_guard", "orcid_conflict")


def test_name_change_and_affiliation_change_keep_one_identity(con):
    raws = _load()
    r1 = _run_paper(con, raws[0])                     # Ana Torres, Lisbon
    r4 = _run_paper(con, raws[3])                     # Carla Mendes (同 ORCID), Porto
    ana_v1 = r1["author_resolution"][1]["author_id"]
    ana_v2 = r4["author_resolution"][0]["author_id"]
    assert ana_v1 == ana_v2                            # 改名 + 换机构仍是同一人
    ident = _ident_map(con)[ana_v2]
    assert ident["name"] == "Carla Mendes"
    assert "Ana Torres" in ident["previous_names"]
    assert {"University of Lisbon", "University of Porto"} <= set(ident["affiliations"])


def test_missing_orcid_with_affiliation_conflict_goes_manual(con):
    raws = _load()
    _run_paper(con, raws[0])                          # 建立 ORCID 版 Wei Zhang
    r3 = _run_paper(con, raws[2])
    guard3 = r3["author_resolution"][0]
    if guard3["status"] == "pending":
        author_id.confirm_review(con, guard3["review_id"], "separate")
    r5 = _run_paper(con, raws[4])                     # 无 ORCID、机构冲突的 Wei Zhang
    res = r5["author_resolution"][0]
    assert res["status"] == "pending"                 # 绝不自动合并
    assert res["reason"] in (
        "affiliation_conflict_guard", "orcid_conflict",
        "ambiguous_candidates", "insufficient_evidence", "below_auto_threshold")
    assert res["author_id"] is None
    pending = author_id.pending_reviews(con)
    assert pending
    # 证据可回放：候选与每个信号都在待审单里
    cand = pending[-1]["candidates"][0]
    assert cand["evidence"]
    assert cand["conflicts"]


def test_orcid_conflict_takes_priority_over_affiliation_guard(con):
    # 署名行自带 ORCID 且与同名既有身份的 ORCID 不一致 => 硬冲突优先
    first = _run_paper(con, {
        "id": "c-1", "doi": "10.7/c1", "title": "C One", "ts": "2025-01-01",
        "authors": [{"name": "Wei Zhang", "orcid": "0000-0002-1825-0097",
                     "affiliations": ["Tsinghua University"]}],
    })
    second = _run_paper(con, {
        "id": "c-2", "doi": "10.7/c2", "title": "C Two", "ts": "2025-02-01",
        "authors": [{"name": "Wei Zhang", "orcid": "0000-0001-5109-3700",
                     "affiliations": ["Other University"]}],
    })
    res = second["author_resolution"][0]
    assert res["status"] == "pending"
    assert res["reason"] == "orcid_conflict"
    assert first["author_resolution"][0]["author_id"] != res.get("author_id")


def test_manual_merge_then_replay_converges(con):
    raws = _load()
    r1 = _run_paper(con, raws[0])
    r5 = _run_paper(con, raws[4])
    review_id = r5["author_resolution"][0]["review_id"]
    target = r1["author_resolution"][0]["author_id"]
    out = author_id.confirm_review(con, review_id, "merge", target_identity=target)
    assert out["status"] == "merged"
    # 重复确认是幂等的，不会再改身份
    again = author_id.confirm_review(con, review_id, "merge", target_identity=target)
    assert again["resolved_identity"] == out["resolved_identity"]
    # 重放同一论文：署名位直接采用人工确认结果
    replay = _run_paper(con, raws[4])
    assert replay["author_resolution"][0]["status"] == "confirmed"
    assert replay["author_resolution"][0]["author_id"] == target


def test_manual_separate_creates_new_identity(con):
    raws = _load()
    _run_paper(con, raws[0])
    r5 = _run_paper(con, raws[4])
    review_id = r5["author_resolution"][0]["review_id"]
    out = author_id.confirm_review(con, review_id, "separate")
    ident = _ident_map(con)[out["resolved_identity"]]
    assert ident["status"] == "confirmed"
    assert ident["name"] == "Wei Zhang"
    assert "No-Name Institute" in ident["affiliations"]


def test_anonymous_byline_isolated_and_never_merged(con):
    raws = _load()
    r6 = _run_paper(con, raws[5])
    anon = r6["author_resolution"][0]
    sara = r6["author_resolution"][1]
    assert anon["status"] == "anonymous"
    assert anon["author_id"].startswith(author_id.ANON_PREFIX)
    assert sara["status"] == "confirmed"
    # 匿名身份存放在独立的 anon 分区
    anon_ident = store.get(con, anon["author_id"])
    assert anon_ident["anonymous"] is True
    # 匿名身份不进入任何具名身份的合作者图
    for ident in _ident_map(con).values():
        assert anon["author_id"] not in ident["coauthors"]


def test_reimport_does_not_change_existing_identity(con):
    raws = _load()
    r1 = _run_paper(con, raws[0])
    wz = r1["author_resolution"][0]["author_id"]
    before = json.dumps(_ident_map(con)[wz], sort_keys=True)
    _run_paper(con, raws[6])                          # 夹具中重复的 p-001
    after = json.dumps(_ident_map(con)[wz], sort_keys=True)
    assert before == after                            # 既有身份字节级不变
    # 旧论文的作者引用仍然稳定
    assert r1["author_resolution"][0]["author_id"] == wz


def test_author_order_is_separate_from_identity(con):
    raws = _load()
    r1 = _run_paper(con, raws[0])                     # Wei Zhang 在 order 0
    r2 = _run_paper(con, raws[1])                     # 换序：Wei Zhang 在 order 1
    ids_1 = [x["author_id"] for x in r1["author_resolution"]]
    ids_2 = [x["author_id"] for x in r2["author_resolution"]]
    assert set(ids_1) == set(ids_2)                   # 身份集合不变
    assert [x["order"] for x in r2["author_resolution"]] == [0, 1]
    wz = r1["author_resolution"][0]["author_id"]
    ident = _ident_map(con)[wz]
    assert {"paper_id": "p-001", "order": 0} in ident["papers"]
    assert {"paper_id": "p-002", "order": 1} in ident["papers"]


def test_interrupted_resume_replays_pending_queue(con):
    raws = _load()
    # 模拟只处理到 p-005 后中断（p-003、p-005 各挂起一张待审单）
    for raw in raws[:5]:
        _run_paper(con, raw)
        store.commit(con)
    before = {p["review_id"] for p in author_id.pending_reviews(con)}
    assert before                                    # 至少一张待审单被持久化
    # 重新跑全量（恢复）：待审单仍在，且不会重复创建
    for raw in raws:
        _run_paper(con, raw)
    after = {p["review_id"] for p in author_id.pending_reviews(con)}
    assert before == after


def test_orcid_conflict_blocks_auto_merge():
    byline = {"order": 0, "name": "Wei Zhang", "orcid": "0000-0002-1825-0097",
              "affiliations": ["X"], "anonymous": False}
    identity = {"identity_id": "author:orcid:0000-0001-0000-0000", "name": "Wei Zhang",
                "orcid": "0000-0001-0000-0000", "previous_names": [], "affiliations": ["X"],
                "positions": [0], "coauthors": []}
    score, evidence, conflicts = author_id.score_candidate(byline, identity, [])
    assert score == 0.0
    assert "orcid_conflict" in conflicts


def test_full_pipeline_papers_idempotent(tmp_path):
    db = tmp_path / "papers.db"
    a = pipeline.run(store.connect(db), path=FIX, kind="papers")
    b = pipeline.run(store.connect(db), path=FIX, kind="papers")
    assert a["fed"] == b["fed"] == 7
    assert a["rows"] == b["rows"]                     # 重复 p-001 不增行
