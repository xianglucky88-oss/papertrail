"""version 子系统验证：版本链、撤回、到达顺序、冲突、候选规则、幂等与半批恢复。

每个用例使用独立临时 sqlite 库；事件流（vx_events）是只追加事实源，
vx_* 其余表均为可由 rebuild() 重算的物化状态。
"""
import pytest

from src.papertrail import store, version
from src.papertrail import doi_map, dedup, normalizer

AUTHORS = ["A. Li", "B. Wang"]
TITLE = "Graphs of Things"


def make_rec(rid, rtype, *, doi="", arxiv="", title=TITLE, authors=None,
             date="", label="", **extra):
    rec = {"id": rid, "record_type": rtype, "title": title,
           "authors": AUTHORS if authors is None else authors,
           "date": date, "version_label": label}
    if doi:
        rec["doi"] = doi
    if arxiv:
        rec["arxiv_id"] = arxiv
    rec.update(extra)
    return rec


def pre(rid, ver, date, arxiv="2401.00001", **extra):
    return make_rec(rid, "preprint", arxiv=arxiv, date=date, label=ver, **extra)


def pub(rid, date="2024-09-01", doi="10.1000/joto.2024.09", **extra):
    return make_rec(rid, "journal", doi=doi, date=date, **extra)


@pytest.fixture()
def con(tmp_path):
    c = store.connect(str(tmp_path / "v.db"))
    yield c
    c.close()


def papers_of(c):
    """当前有效论文（merged 行仅作审计保留，不计为独立论文）。"""
    return store.active_papers(c)


def ingest_all(c, records):
    with version.session(c):
        return [version.ingest(c, r) for r in records]


# --- 1. 多版预印本 + 正式稿归并为一条链 ------------------------------------

def test_multi_preprint_and_published_form_one_chain(con):
    results = ingest_all(con, [
        pre("p1", "v1", "2024-01-15"),
        pre("p2", "v2", "2024-05-20"),
        pub("j1"),
    ])
    assert {r["role"] for r in results} == {"canonical"}
    papers = papers_of(con)
    assert len(papers) == 1
    pid = papers[0]["paper_id"]

    chain = version.version_chain(con, pid)
    kinds = [(n["kind"], n["version_label"]) for n in chain["nodes"]]
    assert kinds == [("preprint", "v1"), ("preprint", "v2"), ("published", "")]
    assert [n["ord"] for n in chain["nodes"]] == [0, 1, 2]
    # supersedes 边按链方向串联
    assert [e["edge_kind"] for e in chain["edges"]] == ["supersedes", "supersedes"]
    ordered_to = [e["to_node"] for e in chain["edges"]]
    assert ordered_to == [chain["nodes"][1]["node_id"], chain["nodes"][2]["node_id"]]

    formal = version.current_formal(con, pid)
    assert formal["record_key"] == version.key_of(pub("j1"))
    assert formal["metadata"]["doi"] == "10.1000/joto.2024.09"
    # 每版元数据完整保留、互不覆盖
    by_label = {n["version_label"]: n for n in chain["nodes"] if n["kind"] == "preprint"}
    assert by_label["v1"]["metadata"]["date"] == "2024-01-15"
    assert by_label["v2"]["metadata"]["date"] == "2024-05-20"
    assert by_label["v1"]["metadata"]["authors"] == AUTHORS


def test_arxiv_version_suffix_is_version_label_not_new_identity(con):
    ingest_all(con, [
        make_rec("p1", "preprint", arxiv="2401.00001v1", date="2024-01-15"),
        make_rec("p2", "preprint", arxiv="2401.00001v2", date="2024-05-20"),
    ])
    assert len(papers_of(con)) == 1
    pid = papers_of(con)[0]["paper_id"]
    idents = {i["id_type"]: i["id_value"] for i in store.identifiers_for_paper(con, pid)}
    assert idents["arxiv"] == "2401.00001"   # 主标识无 vN
    labels = sorted(n["version_label"] for n in store.nodes_for_paper(con, pid))
    assert labels == ["v1", "v2"]


# --- 2. 期刊先于预印本到达：选择只看证据不看到达顺序 ------------------------

@pytest.mark.parametrize("order", [
    ["p1", "p2", "j1"],   # 预印本 -> 正式稿
    ["j1", "p1", "p2"],   # 正式稿先到
    ["p2", "j1", "p1"],   # 乱序
    ["j1", "p2", "p1"],
])
def test_arrival_order_does_not_change_current_formal(con, order):
    bucket = {"p1": pre("p1", "v1", "2024-01-15"),
              "p2": pre("p2", "v2", "2024-05-20"),
              "j1": pub("j1")}
    ingest_all(con, [bucket[k] for k in order])
    assert len(papers_of(con)) == 1
    pid = papers_of(con)[0]["paper_id"]
    formal = version.current_formal(con, pid)
    assert formal["record_key"] == version.key_of(bucket["j1"])
    basis = version.current_formal_basis(con, pid)
    assert basis["selection_principle"] == "evidence_based_not_arrival_order"
    # 链的物化次序同样与到达顺序无关
    chain = version.version_chain(con, pid)
    assert [n["record_key"] for n in chain["nodes"]] == [
        version.key_of(bucket["p1"]), version.key_of(bucket["p2"]),
        version.key_of(bucket["j1"])]


def test_no_published_version_falls_back_to_latest_preprint(con):
    ingest_all(con, [pre("p1", "v1", "2024-01-15"), pre("p2", "v2", "2024-05-20")])
    pid = papers_of(con)[0]["paper_id"]
    assert version.current_formal(con, pid) is None
    basis = version.current_formal_basis(con, pid)
    assert basis["rule"] == "no_published_version"
    current = version.current_version(con, pid)
    assert current["version_label"] == "v2"


# --- 3. 撤回：撤回事实可找回，正式版归属不变但标注 retracted ----------------

def test_retraction_recorded_and_recoverable(con):
    records = [pre("p1", "v1", "2024-01-15"), pub("j1"),
               make_rec("r1", "retraction", doi="10.1000/joto.2024.09",
                        date="2025-02-01", retraction=True,
                        retracts_doi="10.1000/joto.2024.09",
                        retraction_reason="image duplication", title="Retraction: " + TITLE)]
    ingest_all(con, records)
    pid = papers_of(con)[0]["paper_id"]
    rets = version.retractions(con, pid)
    assert len(rets) == 1
    assert rets[0]["reason"] == "image duplication"
    assert rets[0]["retracted_date"] == "2025-02-01"
    assert rets[0]["target_node_id"] == version.current_formal(con, pid)["node_id"]
    # 撤回不改变正式版归属，但依据中必须标注
    formal = version.current_formal(con, pid)
    assert formal["record_key"] == version.key_of(pub("j1"))
    basis = version.current_formal_basis(con, pid)
    assert basis["retracted"] is True
    assert len(basis["retraction_ids"]) == 1
    # retracts 边可找回
    edge_kinds = {e["edge_kind"] for e in version.version_chain(con, pid)["edges"]}
    assert "retracts" in edge_kinds


def test_retraction_arrives_before_target_then_bridges(con):
    # 撤回通知先到（悬空），随后正式稿携带同一 DOI 到达，自动并链并补齐指向
    dangling = make_rec("r0", "retraction", doi="10.1000/joto.2024.09",
                        date="2025-02-01", retraction=True,
                        retraction_reason="image duplication", title="Retraction notice")
    r1 = ingest_all(con, [dangling])[0]
    assert r1["dangling"] is True
    pid = r1["paper_id"]
    assert store.get_paper(con, pid)["status"] == "retracted"

    ingest_all(con, [pub("j1")])
    assert len(papers_of(con)) == 1
    rets = version.retractions(con, pid)
    assert len(rets) == 1
    assert rets[0]["target_node_id"] == version.current_formal(con, pid)["node_id"]


# --- 4. 元数据冲突：保留差异事件，两版元数据都不丢 -------------------------

def test_metadata_conflict_is_kept_without_overwriting(con):
    good = pub("j1", date="2024-09-01")
    conflicted = dict(pub("j2", date="2024-10-01"),
                      title="Graphs of Things (Extended)", authors=AUTHORS)
    ingest_all(con, [good, conflicted])
    pid = papers_of(con)[0]["paper_id"]
    history = version.history(con, pid)
    conflict_events = [e for e in history if e["kind"] == "metadata_conflict"]
    assert conflict_events
    diffs = conflict_events[0]["payload"]["conflicts"]
    fields = {d["field"] for d in diffs}
    assert {"title", "date"} <= fields
    # 两个版本节点都在，各自元数据原样保留
    nodes = store.nodes_for_paper(con, pid)
    titles = {n["metadata"]["title"] for n in nodes}
    assert {"Graphs of Things", "Graphs of Things (Extended)"} <= titles


def test_conflicting_dates_keep_deterministic_formal_under_all_orders(con):
    a = pub("j1", date="2024-09-01")
    b = pub("j2", date="2025-03-01", doi="10.1000/joto.2024.09")
    for first, second in ((a, b), (b, a)):
        c = store.connect(":memory:")
        ingest_all(c, [first, second])
        pid = store.all_papers(c)[0]["paper_id"]
        formal = version.current_formal(c, pid)
        assert formal["node_date"] == "2025-03-01"   # 日期晚的胜出，与到达顺序无关
        assert len(version.history(c, pid)) >= 1
        c.close()


# --- 5. 证据不足：候选保留规则 ---------------------------------------------

def test_weak_evidence_keeps_separate_papers_and_candidates(con):
    # 同标题、有共同作者，但 DOI 明确不同 -> W3，不归并
    a = pub("j1", doi="10.1000/a.2024.01")
    b = pub("j2", doi="10.1000/b.2024.02")
    ingest_all(con, [a, b])
    assert len(papers_of(con)) == 2
    cands = version.candidates(con)
    assert len(cands) == 1
    assert cands[0]["reason"] == version.CANDIDATE_IDENT_CONFLICT
    assert cands[0]["evidence"]["existing_doi"] != cands[0]["evidence"]["incoming_doi"]


def test_title_only_match_is_w2_candidate(con):
    a = pub("j1", doi="10.1000/a", authors=["X. Solo"])
    b = pub("j2", doi="10.1000/b", authors=["Y. Solo"])
    ingest_all(con, [a, b])
    assert len(papers_of(con)) == 2
    assert version.candidates(con)[0]["reason"] == version.CANDIDATE_TITLE_ONLY


def test_shared_author_with_conflicting_ids_is_w3_and_rule_text_persisted(con):
    a = pub("j1", doi="10.1000/a", authors=["A. Li", "B. Wang"])
    b = pub("j2", doi="10.1000/b", authors=["A. Li", "C. Chen"])  # 作者集合不同 -> 非 R3
    ingest_all(con, [a, b])
    cand = version.candidates(con)[0]
    assert cand["reason"] == version.CANDIDATE_IDENT_CONFLICT
    # 规则说明随事件持久化，供人工复核与历史重算
    retained = [e for e in version.history(con, cand["paper_id"])
                if e["kind"] == "candidate_retained"]
    assert retained[0]["payload"]["rule_description"] == \
        version.CANDIDATE_RULES[version.CANDIDATE_IDENT_CONFLICT]


def test_no_anchor_shared_author_is_w1_candidate(con):
    # 双方都没有 DOI/arXiv，仅标题+部分共有作者 -> W1，保持两条独立链
    a = make_rec("t1", "journal", title=TITLE, authors=["A. Li", "B. Wang"], date="2024-01-01")
    b = make_rec("t2", "journal", title=TITLE, authors=["A. Li", "C. Chen"], date="2024-02-01")
    ingest_all(con, [a, b])
    assert len(papers_of(con)) == 2
    assert version.candidates(con)[0]["reason"] == version.CANDIDATE_TITLE_AUTHOR


def test_identical_title_and_authors_without_ids_merges_via_r3(con):
    # 无任何强标识，但标题与作者集合完全一致 -> R3 仍可自动接链
    a = make_rec("t1", "preprint", title=TITLE, authors=AUTHORS,
                 date="2024-01-01", label="v1")
    b = make_rec("t2", "preprint", title=TITLE, authors=AUTHORS,
                 date="2024-03-01", label="v2")
    ingest_all(con, [a, b])
    assert len(papers_of(con)) == 1
    assert version.candidates(con) == []
    pid = papers_of(con)[0]["paper_id"]
    assert pid.startswith("paper:fp:")                         # 指纹锚点兜底
    assert len(store.nodes_for_paper(con, pid)) == 2


def test_bridge_record_merges_candidate_papers(con):
    # 两条各自独立的链，仅标题相似（候选）；第三条记录同时携带双方标识 -> 桥接归并
    a = pre("p1", "v1", "2024-01-01", arxiv="2401.00001", authors=AUTHORS)
    b = pub("j1", doi="10.1000/joto.2024.09",
            title=TITLE + "?", authors=["C. Other"], date="2024-09-01")
    ingest_all(con, [a, b])
    assert len(papers_of(con)) == 2
    bridge = make_rec("j2", "journal", doi="10.1000/joto.2024.09",
                      arxiv="2401.00001", date="2024-09-01")
    ingest_all(con, [bridge])
    papers = papers_of(con)
    assert len(papers) == 1
    pid = papers[0]["paper_id"]
    assert pid.startswith("paper:doi:")          # DOI 锚定优先级最高
    # 旧链节点已整体并入，且保留全部历史
    assert len(store.nodes_for_paper(con, pid)) == 3
    merges = version.merge_history(con, pid)
    assert len(merges) == 1
    assert merges[0]["payload"]["rules"]
    # 已归并的候选不再悬挂
    assert version.candidates(con, pid) == []


# --- 6. 重复运行：幂等，不产生重复节点/事件 --------------------------------

def test_repeated_ingest_is_idempotent(con):
    records = [pre("p1", "v1", "2024-01-15"), pre("p2", "v2", "2024-05-20"), pub("j1")]
    ingest_all(con, records)
    events_before = len(store.all_events(con))
    nodes_before = len(store.nodes_for_paper(con, papers_of(con)[0]["paper_id"]))

    again = ingest_all(con, records)               # 整批重跑
    assert all(r.get("idempotent") for r in again)
    pid = papers_of(con)[0]["paper_id"]
    assert len(store.nodes_for_paper(con, pid)) == nodes_before
    assert len(store.all_events(con)) == events_before


def test_same_version_from_another_source_is_marked_duplicate(con):
    a = pre("p1", "v1", "2024-01-15")
    mirror = dict(pre("p1-mirror", "v1", "2024-01-15"))
    mirror["id"] = "p1-mirror"
    ingest_all(con, [a, mirror])
    pid = papers_of(con)[0]["paper_id"]
    # 同 kind/label/date 不新建节点，索引为 duplicate
    assert len(store.nodes_for_paper(con, pid)) == 1
    assert version.is_duplicate_record(con, version.key_of(mirror)) is True
    dup_events = [e for e in version.history(con, pid) if e["kind"] == "duplicate_record"]
    assert len(dup_events) == 1


# --- 7. 半批失败恢复：异常回滚，重跑收敛到正确当前版本 ----------------------

def test_half_batch_failure_rolls_back_and_rerun_converges(con):
    good = [pre("p1", "v1", "2024-01-15"), pre("p2", "v2", "2024-05-20")]
    ingest_all(con, good)
    pid = papers_of(con)[0]["paper_id"]
    state_before = (len(store.all_events(con)),
                    len(store.nodes_for_paper(con, pid)))

    # 第二批中途抛错：session 必须整体回滚，不留半截物化状态
    # j2 与 j1 同 DOI 但元数据日期冲突（同一条链的两个正式版元数据快照）
    j2 = pub("j2", doi="10.1000/joto.2024.09", date="2024-10-01")
    with pytest.raises(RuntimeError):
        with version.session(con):
            version.ingest(con, pub("j1"))
            version.ingest(con, j2)
            raise RuntimeError("simulated half-batch failure")

    assert len(store.all_events(con)) == state_before[0]
    assert len(store.nodes_for_paper(con, pid)) == state_before[1]
    assert version.current_formal(con, pid) is None

    # 重跑完整批次：确定性 event_id 保证收敛，当前正式版取日期更晚的快照
    ingest_all(con, [pub("j1"), j2])
    # DOI 到达后 paper 已改名（paper:arxiv -> paper:doi），经别名链解析
    canonical_pid = store.canonical_paper_id(con, pid)
    formal = version.current_formal(con, canonical_pid)
    assert formal["node_date"] == "2024-10-01"
    assert len(papers_of(con)) == 1
    # 冲突的两版元数据各自保留为独立节点（同 DOI 不同日期 = 两个元数据快照），
    # 冲突字段经 metadata_conflict 事件留痕，旧版元数据不被覆盖。
    published_dates = sorted(n["node_date"] for n in
                             store.nodes_for_paper(con, canonical_pid)
                             if n["kind"] == "published")
    assert published_dates == ["2024-09-01", "2024-10-01"]
    assert any(e["kind"] == "metadata_conflict"
               for e in version.history(con, canonical_pid))


# --- 8. 历史格式与 rebuild 重算 --------------------------------------------

def test_event_history_shape_and_rebuild_reproduces_state(con):
    ingest_all(con, [pre("p1", "v1", "2024-01-15"), pub("j1"),
                     make_rec("r1", "retraction", doi="10.1000/joto.2024.09",
                              date="2025-02-01", retraction=True,
                              retraction_reason="issue", title="retr")])
    pid = papers_of(con)[0]["paper_id"]
    history = version.history(con, pid)
    kinds = {e["kind"] for e in history}
    # 历史格式稳定：事件五要素齐全
    for e in history:
        assert set(e) >= {"event_id", "paper_id", "seq", "kind", "payload", "ingest_ts"}
        assert isinstance(e["seq"], int)
    assert {"paper_created", "identifier_seen", "node_seen",
            "formal_selected", "retraction_recorded"} <= kinds
    formal_selected = [e for e in history if e["kind"] == "formal_selected"]
    assert formal_selected[-1]["payload"]["basis"]["selection_principle"] == \
        "evidence_based_not_arrival_order"

    snapshot = {
        "nodes": [(n["node_id"], n["ord"], n["kind"]) for n in store.nodes_for_paper(con, pid)],
        "edges": [(e["from_node"], e["to_node"], e["edge_kind"])
                  for e in store.edges_for_paper(con, pid)],
        "formal": version.current_formal(con, pid)["node_id"],
        "retractions": len(version.retractions(con, pid)),
        "records": sorted(r[0] for r in con.execute("SELECT record_key FROM vx_record_index")),
    }
    original_event_count = len(store.all_events(con))
    summary = version.rebuild(con)
    # 原始事件流完整保留，仅追加 formal_recomputed 审计事件
    assert summary["replayed_events"] == original_event_count
    assert len(store.all_events(con)) >= original_event_count
    assert {e["kind"] for e in store.all_events(con)} >= {
        "paper_created", "node_seen", "formal_selected", "formal_recomputed"}
    rebuilt = {
        "nodes": [(n["node_id"], n["ord"], n["kind"]) for n in store.nodes_for_paper(con, pid)],
        "edges": [(e["from_node"], e["to_node"], e["edge_kind"])
                  for e in store.edges_for_paper(con, pid)],
        "formal": version.current_formal(con, pid)["node_id"],
        "retractions": len(version.retractions(con, pid)),
        "records": sorted(r[0] for r in con.execute("SELECT record_key FROM vx_record_index")),
    }
    assert rebuilt == snapshot


# --- 9. doi_map / dedup 阶段集成 -------------------------------------------

def _stage(rec, stage_mod, con):
    rec = normalizer.process(rec)
    with version.session(con):
        rec = version.process(rec)
        rec = stage_mod.process(rec)
        rec = dedup.process(rec) if stage_mod is doi_map else doi_map.process(rec)
    return rec


def test_doi_map_anchors_preprint_to_formal_identity(con):
    with version.session(con):
        p = version.process(normalizer.process(pre("p1", "v1", "2024-01-15")))
        j = version.process(normalizer.process(pub("j1")))
        p = doi_map.process(p)
        j = doi_map.process(j)
    assert p["_canonical_paper_id"] == j["_canonical_paper_id"]
    # 预印本记录的主标识也解析到正式版 DOI（先到正式稿同样成立）
    assert p["_canonical_doi"] == "10.1000/joto.2024.09"
    assert p["_canonical_formal_basis_rule"].startswith("latest_published")


def test_dedup_flags_duplicate_but_keeps_weak_candidates_distinct(con):
    with version.session(con):
        a = version.process(normalizer.process(pre("p1", "v1", "2024-01-15")))
        mirror = version.process(normalizer.process(
            pre("p1-mirror", "v1", "2024-01-15")))
        other = version.process(normalizer.process(
            pub("x1", doi="10.2000/other", authors=["Z. Stranger"])))
        a, mirror, other = dedup.process(a), dedup.process(mirror), dedup.process(other)
    assert a["_is_duplicate"] is False
    assert mirror["_is_duplicate"] is True
    assert other["_is_duplicate"] is False
    assert other["_dedup_paper_id"] != a["_dedup_paper_id"]


def test_non_scholarly_records_follow_legacy_stages(con):
    legacy = {"id": "r-001", "src": " Alpha ", "name": " Legacy ", "value": 5}
    with version.session(con):
        out = version.process(normalizer.process(legacy))
        out = doi_map.process(out)
        out = dedup.process(out)
    assert out["_stage_version"] == "skipped_non_scholarly"
    assert out["_stage_doi_map"] == "naive"
    assert out["_stage_dedup"] == "ok"
    assert out["src"] == "alpha" and out["name"] == "Legacy" and out["value"] == 5


# --- 10. 选择依据持久化，可用于历史重算 ------------------------------------

def test_formal_basis_persists_evidence_and_is_reusable(con):
    ingest_all(con, [pre("p1", "v1", "2024-01-15"), pub("j1", date="2024-09-01"),
                     pub("j3", date="2024-11-01")])
    pid = papers_of(con)[0]["paper_id"]
    basis = version.current_formal_basis(con, pid)
    assert basis["selected"] == version.current_formal(con, pid)["node_id"]
    # 依据中保留全部候选节点与排序输入，规则变化时可据此重算
    assert len(basis["candidates"]) == 2
    assert all("node_date" in c and "version_label" in c for c in basis["candidates"])
    selections = [e for e in version.history(con, pid) if e["kind"] == "formal_selected"]
    assert len(selections) >= 1
