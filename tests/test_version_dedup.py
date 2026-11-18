"""版本链与去重键对作者采用结果的联动测试。

同一作者的合并会改变版本链与去重键；待人工状态下两者都保持 guarded/可回放。
"""
import inspect
import json
from pathlib import Path

import pytest

from src.papertrail import author_id, store, pipeline, version, dedup
from src.papertrail.models import Context

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "papers.json"


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "chain.db")
    yield c
    c.close()


def _full(con, raw):
    ctx = Context(con=con)
    rec = raw
    for name in ["normalizer", "harvest", "author_id", "doi_map", "version", "dedup"]:
        mod = __import__("src.papertrail." + name, fromlist=["process"])
        rec = (mod.process(rec, ctx)
               if len(inspect.signature(mod.process).parameters) >= 2
               else mod.process(rec))
    return rec


def _load():
    return json.loads(FIX.read_text(encoding="utf-8"))


def test_preprint_and_article_share_chain_despite_reordered_authors(con):
    raws = _load()
    pre = _full(con, raws[0])       # p-001
    art = _full(con, raws[1])       # p-002，换序 + 显式 version_of
    assert pre["version"]["status"] == "chain_head"
    assert art["version"]["status"] == "chained"
    assert art["version"]["chain_id"] == pre["version"]["chain_id"]
    assert art["version"]["parent_id"] == "p-001"
    # 成链证据是可回放的信号列表，而非裸相似度
    tags = [tag for tag, _w in art["version"]["evidence"]]
    assert "doi_version_link" in tags
    assert "canonical_author_overlap:1.00" in tags
    chain = store.get(con, art["version"]["chain_id"])
    assert chain["members"] == ["p-001", "p-002"]


def test_homonyms_get_different_dedup_fingerprints(con):
    raws = _load()
    # 冷启动顺序：两个 Wei Zhang 各自独立建身份，再比较去重指纹
    r3 = _full(con, raws[2])        # 无 ORCID 海洋大学 Wei Zhang
    r1 = _full(con, raws[0])        # ORCID 清华 Wei Zhang
    assert r1["dedup"]["status"] == "unique"
    assert r3["dedup"]["status"] == "unique"
    assert r1["dedup"]["key"] != r3["dedup"]["key"]


def test_pending_author_keeps_dedup_guarded(con):
    raws = _load()
    _full(con, raws[0])
    r5 = _full(con, raws[4])        # 机构/ORCID 冲突 -> 待人工
    assert r5["dedup"]["status"] == "guarded"
    assert r5["dedup"]["pending_orders"] == [0]


def test_manual_merge_changes_dedup_key_and_replays(con):
    raws = _load()
    r1 = _full(con, raws[0])
    r5 = _full(con, raws[4])
    guarded_key = r5["dedup"]["key"]
    review = author_id.pending_reviews(con)[0]
    target = r1["author_resolution"][0]["author_id"]
    author_id.confirm_review(con, review["review_id"], "merge", target_identity=target)
    store.commit(con)
    # 重放 p-005：身份被采用后，去重从 guarded 变为确定结论
    replay = _full(con, raws[4])
    assert replay["dedup"]["status"] in ("unique", "duplicate")
    # 去重键随 canonical 作者身份变化而确定性改变
    assert replay["dedup"]["key"] != guarded_key
    # 再重放一次结论稳定（门槛可回放，不依赖瞬时相似度）
    replay2 = _full(con, raws[4])
    assert replay2["dedup"]["key"] == replay["dedup"]["key"]


def test_reimport_keeps_version_and_dedup_stable(con):
    raws = _load()
    first = _full(con, raws[0])
    second = _full(con, raws[6])   # 重复 p-001
    assert second["dedup"]["key"] == first["dedup"]["key"]
    assert second["version"]["chain_id"] == first["version"]["chain_id"]


def test_version_evidence_threshold_is_replayable(con):
    # 无显式 DOI 链接、作者证据不足时不得自动成链
    head = _full(con, {
        "id": "x-1", "doi": "10.9/x1", "title": "Solo Work on Tokens", "ts": "2024-01-01",
        "authors": [{"name": "Solo Author", "orcid": "", "affiliations": ["Lab A"]}],
    })
    follower = _full(con, {
        "id": "x-2", "doi": "10.9/x2", "title": "Solo Work on Tokens", "ts": "2024-08-01",
        "authors": [{"name": "Solo Author", "orcid": "", "affiliations": ["Lab B"]}],
    })
    assert follower["version"]["status"] == "pending_link"
    assert follower["version"]["parent_id"] is None
    assert follower["version"]["score"] < version.AUTO_CHAIN_SCORE
