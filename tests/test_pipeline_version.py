"""端到端：学术记录走完整流水线（version → doi_map → dedup → keyer → store）。

使用独立临时库与临时 fixtures，避免污染默认 papertrail.db；
覆盖：期刊先到、多版预印本、撤回、重复事件、重跑幂等。
"""
import json

import pytest

from src.papertrail import pipeline, store, version


def _records():
    base = {"title": "Graphs of Things", "authors": ["A. Li", "B. Wang"]}
    return [
        {"id": "e1", **base, "record_type": "journal",
         "doi": "10.1000/joto.2024.09", "date": "2024-09-01"},          # 正式稿先到
        {"id": "e2", **base, "record_type": "preprint",
         "arxiv_id": "2401.00001v1", "date": "2024-01-15"},
        {"id": "e3", **base, "record_type": "preprint",
         "arxiv_id": "2401.00001v2", "date": "2024-05-20"},
        {"id": "e4", **base, "record_type": "retraction", "retraction": True,
         "doi": "10.1000/joto.2024.09", "date": "2025-02-01",
         "title": "Retraction: Graphs of Things",
         "retraction_reason": "image duplication"},
        {"id": "e2-mirror", **base, "record_type": "preprint",   # 重复事件（不同来源 id）
         "arxiv_id": "2401.00001v1", "date": "2024-01-15"},
    ]


@pytest.fixture()
def patched_env(tmp_path, monkeypatch):
    fix = tmp_path / "input.json"
    fix.write_text(json.dumps(_records()), encoding="utf-8")
    db = tmp_path / "e2e.db"
    # pipeline.run() 在无参时自行 connect，必须打补丁到它实际引用的模块属性
    monkeypatch.setattr("src.papertrail.pipeline._store.DB_PATH", db)
    monkeypatch.setattr("src.papertrail.pipeline._loader.FIXTURES", fix)
    monkeypatch.setattr("src.papertrail.config.FIXTURES", fix)
    monkeypatch.setattr("src.papertrail.config.DB_PATH", db)
    return db


def test_full_pipeline_scholarly_versioning(patched_env):
    first = pipeline.run()
    assert first["fed"] == 5
    second = pipeline.run()                                # 重复运行
    assert second["rows"] == first["rows"] == 5            # 幂等：不增行

    con = store.connect(str(patched_env))
    try:
        active = store.active_papers(con)
        assert len(active) == 1
        pid = active[0]["paper_id"]
        assert pid.startswith("paper:doi:")                    # DOI 为最强锚点
        idents = {i["id_type"]: i["id_value"]
                  for i in store.identifiers_for_paper(con, pid)}
        assert idents["doi"] == "10.1000/joto.2024.09"
        assert idents["arxiv"] == "2401.00001"

        # 主标识：预印本记录也解析到正式版 paper 与正式版 DOI
        rows = dict(store.all_records(con))
        pre_row = rows["e2"]                                        # keyer 以记录 id 为键
        assert pre_row["_canonical_paper_id"] == pid
        assert pre_row["_canonical_doi"] == "10.1000/joto.2024.09"

        # 链：两版预印本 + 正式稿 + 撤回，次序与到达顺序无关
        chain = version.version_chain(con, pid)
        kinds = [n["kind"] for n in chain["nodes"]]
        assert kinds == ["preprint", "preprint", "published", "retraction"]
        edge_kinds = {e["edge_kind"] for e in chain["edges"]}
        assert {"supersedes", "retracts"} <= edge_kinds

        # 当前正式版可查，撤回事实与每版元数据可找回
        formal = version.current_formal(con, pid)
        assert formal["metadata"]["doi"] == "10.1000/joto.2024.09"
        basis = version.current_formal_basis(con, pid)
        assert basis["retracted"] is True
        assert version.retractions(con, pid)[0]["reason"] == "image duplication"
        labels = sorted(n["version_label"] for n in chain["nodes"]
                        if n["kind"] == "preprint")
        assert labels == ["v1", "v2"]

        # 重复事件：镜像记录被 dedup 标记，且不产生第二个节点
        mirror_row = rows["e2-mirror"]
        assert mirror_row["_is_duplicate"] is True
        assert mirror_row["_dedup_paper_id"] == pid
        assert rows["e1"]["_dedup_paper_id"] == pid
        node_count = len(store.nodes_for_paper(con, pid))
        assert node_count == 4                                      # 2 预印本+正式稿+撤回
    finally:
        con.close()


def test_pipeline_half_batch_failure_then_converges(patched_env):
    con = store.connect(str(patched_env))
    # 先成功摄取前三条（正式稿 + 两版预印本）
    good = _records()[:3]
    with version.session(con):
        for r in good:
            version.ingest(con, r)
    before_events = len(store.all_events(con))

    # 模拟半批失败：撤回写到一半抛错，整体回滚
    with pytest.raises(RuntimeError):
        with version.session(con):
            version.ingest(con, _records()[3])
            raise RuntimeError("disk full")
    assert len(store.all_events(con)) == before_events

    # 补跑完整剩余批次后状态与一次成功完全一致
    with version.session(con):
        for r in _records()[3:]:
            version.ingest(con, r)
    pid = store.active_papers(con)[0]["paper_id"]
    assert len(version.retractions(con, pid)) == 1
    assert version.current_formal_basis(con, pid)["retracted"] is True
    con.close()
