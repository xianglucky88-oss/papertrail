"""doi_map 标识层测试：规范化、别名映射、冲突、失效链接、批量重试、可重放。

兼容两种运行方式（本环境无 pytest 时用第二种）：
    PYTHONPATH=. python3 -m pytest tests/test_doi_map.py -q
    python3 -I -m unittest discover -s tests -p "test_doi_map.py" -v
全部使用内存 mapper，测试之间完全隔离，不写本地 db 文件。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from papertrail import doi_map as dm  # noqa: E402
from papertrail import harvest, dedup  # noqa: E402

try:
    from .conftest import in_memory_mapper
except ImportError:  # 直接以顶层模块方式运行时
    from conftest import in_memory_mapper


class NormalizationTests(unittest.TestCase):
    def test_url_case_prefix_proxy_all_map_to_same_key(self):
        raws = [
            "https://doi.org/10.1000/XYZ.123",
            "http://dx.doi.org/10.1000/xyz.123",
            "doi:10.1000/XYZ.123",
            "DOI: 10.1000/xyz.123",
            "10.1000/XYZ.123",
            "https://proxy.example.edu/login?url=https://doi.org/10.1000/xyz.123",
        ]
        keys = {dm.normalize_doi(r).stable_key for r in raws}
        self.assertEqual(keys, {"doi:10.1000/xyz.123"})

    def test_raw_evidence_is_never_dropped(self):
        raw = "https://doi.org/10.1000/ABC"
        res = dm.normalize_doi(raw)
        self.assertEqual(res.raw, raw)           # 原始编号证据保留
        self.assertEqual(res.normalized, "10.1000/abc")
        self.assertEqual(res.via_host, "doi.org")  # URL 来源可追踪

    def test_url_decoding_and_trailing_punctuation(self):
        res = dm.normalize_doi("http://dx.doi.org/10.1/Foo%20Bar).")
        self.assertTrue(res.valid)
        self.assertEqual(res.normalized, "10.1/foo bar")

    def test_arxiv_url_old_id_and_versions(self):
        r1 = dm.normalize_arxiv("https://arxiv.org/abs/2101.00001v3")
        r2 = dm.normalize_arxiv("arXiv:hep-th/9901001")
        r3 = dm.normalize_arxiv("cond-mat/0101002v2")
        self.assertEqual((r1.base, r1.version), ("2101.00001", "v3"))
        self.assertTrue(r2.valid and r2.base == "hep-th/9901001")
        # 不同版本共享同一稳定主键（历史键保住的基础）
        self.assertEqual(r1.stable_key,
                         dm.normalize_arxiv("2101.00001v9").stable_key)
        self.assertEqual(r3.base, "cond-mat/0101002")

    def test_bad_doi_is_invalid_but_deterministic(self):
        res = dm.normalize_doi("garbage-value")
        self.assertFalse(res.valid)
        self.assertEqual(res.kind, dm.KIND_INVALID)
        # 同样的错误写法得到同样的键
        self.assertEqual(res.stable_key,
                         dm.normalize_doi("garbage-value").stable_key)
        self.assertTrue(res.stable_key.startswith("invalid:"))

    def test_empty_and_whitespace(self):
        for blank in ("", "   ", None):
            res = dm.normalize_doi(blank)
            self.assertFalse(res.valid)
            self.assertEqual(res.reason, "empty")

    def test_hint_based_normalize_identifier(self):
        self.assertEqual(
            dm.normalize_identifier("https://arxiv.org/abs/1234.5678").kind,
            dm.KIND_ARXIV)
        self.assertEqual(
            dm.normalize_identifier("10.2/x", hint=dm.KIND_DOI).kind,
            dm.KIND_DOI)


class FieldExtractionTests(unittest.TestCase):
    def test_crossref_nested_identifier_shapes(self):
        rec = {
            "DOI": "10.1000/ABC",
            "link": [{"URL": "https://doi.org/10.1000/abc", "type": "text/html"}],
            "identifier": [
                {"type": "DOI", "value": "10.1000/ABC"},
                {"@term": "arxiv", "@value": "2101.00001"},
            ],
        }
        norms = dm.extract_identifiers(rec)
        kinds = {(n.kind, n.valid) for n in norms}
        self.assertIn((dm.KIND_DOI, True), kinds)
        self.assertIn((dm.KIND_ARXIV, True), kinds)

    def test_arxiv_atom_fields(self):
        rec = {"arXiv_id": "arXiv:2101.00001",
               "journal_ref": "Nat. Phys. 2022"}
        norms = dm.extract_identifiers(rec)
        self.assertEqual(len(norms), 1)
        self.assertEqual(norms[0].base, "2101.00001")

    def test_generic_url_does_not_create_phantom_invalid(self):
        norms = dm.extract_identifiers(
            {"url": "http://dx.doi.org/10.1000/old"})
        self.assertEqual(len(norms), 1)
        self.assertTrue(norms[0].valid)

    def test_record_primary_id_is_not_treated_as_doi(self):
        # 基线夹具里的 "id": "r-001" 是记录主键，不是文献标识
        self.assertEqual(dm.extract_identifiers({"id": "r-001"}), [])

    def test_cross_source_url_is_retyped_by_content(self):
        # DOI 字段里混入 arXiv URL：按内容识别为 arxiv，不冤判 invalid
        norms = dm.extract_identifiers(
            {"doi": "https://arxiv.org/abs/1234.5678"})
        self.assertEqual(len(norms), 1)
        self.assertEqual(norms[0].kind, dm.KIND_ARXIV)
        self.assertTrue(norms[0].valid)
        # 真正的脏值仍然隔离
        bad = dm.extract_identifiers({"doi": "garbage"})
        self.assertEqual(len(bad), 1)
        self.assertFalse(bad[0].valid)

    def test_nested_resource_primary_url(self):
        norms = dm.extract_identifiers(
            {"resource": {"primary_url": "https://doi.org/10.5/q"}})
        self.assertEqual(norms[0].normalized, "10.5/q")
        self.assertTrue(norms[0].valid)


class MapperCoreTests(unittest.TestCase):
    def setUp(self):
        self.m = dm.IdentifierMapper(":memory:")

    def test_stable_key_across_mixed_writings(self):
        raws = ["10.1/a", "https://doi.org/10.1/A", "doi: 10.1/a"]
        keys = [self.m.register(dm.normalize_doi(r), source="s") for r in raws]
        self.assertEqual(len(set(keys)), 1)
        aliases = self.m.aliases_for(keys[0])
        self.assertEqual({a["raw"] for a in aliases}, set(raws))  # 证据全保留

    def test_conflicting_metadata_is_tracked_not_overwritten(self):
        self.m.register(dm.normalize_doi("10.1/a"), source="crossref",
                        metadata={"title": "Original Title", "type": "journal"})
        self.m.register(dm.normalize_doi("10.1/A"), source="aggregator",
                        metadata={"title": "Changed Title", "type": "journal"})
        row = self.m.con.execute(
            "SELECT * FROM identifiers WHERE key=?", ("doi:10.1/a",)).fetchone()
        self.assertEqual(json.loads(row["metadata_json"])["title"],
                         "Original Title")          # 首锚未被覆盖
        self.assertGreaterEqual(row["conflict"], 1)  # 冲突计数可追踪
        conflicts = [e for e in self.m.events("conflict")]
        self.assertTrue(conflicts)
        detail = json.loads(conflicts[0]["detail_json"])
        self.assertEqual(detail["fields"]["title"]["incoming"], "Changed Title")

    def test_dead_link_does_not_change_identity_legacy_policy(self):
        key = self.m.register(dm.normalize_doi("10.1/a"), source="s")
        self.m.register(dm.normalize_doi("https://doi.org/10.1/a"),
                        source="s", dead=True)
        # 身份键不变，别名标 dead，历史键仍可解析
        self.assertEqual(self.m.resolve("https://doi.org/10.1/a", "s"), key)
        alias = self.m.aliases_for(key)[0]
        self.assertIn(alias["status"], (dm.STATUS_ACTIVE, dm.STATUS_DEAD))
        dead_ev = self.m.events("dead")
        self.assertTrue(dead_ev)

    def test_dead_link_pending_policy_goes_to_review(self):
        m = dm.IdentifierMapper(":memory:", dead_link_policy=dm.DEAD_PENDING)
        rec = dm.process({"id": "p1", "src": "legacy",
                          "url": "http://dx.doi.org/10.9/x",
                          "dead_link": True}, m)
        self.assertIn("dead_alias", rec["doi_review"])
        pending = m.pending_aliases()
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0]["key"])       # 未绑定身份键
        # 但记录仍有确定性的“若确认将指向”的键，且原始证据保留
        self.assertEqual(rec["doi_key"], "doi:10.9/x")
        self.assertEqual(pending[0]["raw"],
                         "http://dx.doi.org/10.9/x")

    def test_legacy_policy_and_pending_policy_are_both_replayable(self):
        # 两种策略各自重放，结果各自稳定且可解释（不靠覆盖字段）
        for policy in (dm.DEAD_LEGACY, dm.DEAD_PENDING):
            m = dm.IdentifierMapper(":memory:", dead_link_policy=policy)
            rec1 = dm.process({"id": "x", "src": "s",
                               "doi": "10.1/dead", "dead_link": True}, m)
            snap = json.dumps(m.snapshot(), sort_keys=True)
            rec2 = dm.process({"id": "x", "src": "s",
                               "doi": "10.1/dead", "dead_link": True}, m)
            self.assertEqual(rec1["doi_review"], rec2["doi_review"])
            self.assertEqual(json.dumps(m.snapshot(), sort_keys=True), snap)

    def test_supersede_keeps_historical_key(self):
        old = dm.normalize_doi("10.1/old")
        new = dm.normalize_doi("10.1/new")
        old_key, new_key = self.m.supersede(old, new, source="crossref")
        # 旧写法仍解析到旧键（历史键保住），代表键沿链升级到新键
        self.assertEqual(self.m.resolve("10.1/old", "crossref"), old_key)
        self.assertEqual(self.m.canonical_key(old_key), new_key)
        chain = self.m.version_chain(old_key)
        self.assertEqual(chain[0]["relation"], "supersede")

    def test_arxiv_versions_build_version_chain(self):
        self.m.register(dm.normalize_arxiv("2101.00001v1"), source="arxiv")
        self.m.register(dm.normalize_arxiv("2101.00001v3"), source="arxiv")
        edges = self.m.version_chain("arxiv:2101.00001")
        self.assertEqual({e["from_key"] for e in edges},
                         {"arxiv:2101.00001:v1", "arxiv:2101.00001:v3"})

    def test_invalid_doi_is_quarantined_with_evidence(self):
        rec = dm.process({"id": "i", "src": "crossref",
                          "doi": "not-a-doi"}, self.m)
        self.assertEqual(rec["_stage_doi_map"], "review")
        self.assertIn("invalid_identifier", rec["doi_review"])
        self.assertTrue(rec["doi_key"].startswith("invalid:"))
        self.assertEqual(rec["doi_evidence"][0]["raw"], "not-a-doi")

    def test_preprint_and_doi_merge_group_is_order_independent(self):
        dm.process({"id": "a", "src": "crossref", "DOI": "10.1/a",
                    "arxiv_id": "2101.00001"}, self.m)
        later = dm.process({"id": "b", "src": "arxiv",
                            "arxiv_id": "2101.00001v9"}, self.m)
        group = self.m.same_work_keys(later["doi_key"])
        self.assertEqual(set(group),
                         {"arxiv:2101.00001", "doi:10.1/a"})

    def test_mixed_valid_and_invalid_writings_keep_primary_aligned(self):
        # 同条记录里一个合法 DOI + 一个错误写法：主键必须取合法 DOI，
        # 不允许因列表错位而取到 invalid 键
        rec = dm.process({
            "id": "mix", "src": "crossref",
            "DOI": "10.1/real",
            "identifier": [{"value": "broken-value"}],
        }, self.m)
        self.assertEqual(rec["doi_key"], "doi:10.1/real")
        self.assertEqual(rec["_stage_doi_map"], "ok")
        self.assertIn("invalid_identifier", rec["doi_review"])
        self.assertEqual(len(rec["doi_evidence"]), 2)


class BatchAndRecoveryTests(unittest.TestCase):
    def _items(self):
        return [
            {"raw": "10.1/a", "metadata": {"title": "A"}},
            {"raw": "https://doi.org/10.1/B", "metadata": {"title": "B"}},
            {"raw": "arxiv:2101.00001v2"},
            {"raw": "10.1/c", "supersedes": "10.1/old-c"},
        ]

    def test_batch_retry_resumes_and_skips_completed(self):
        m = dm.IdentifierMapper(":memory:")
        state = m.run_batch("b1", self._items(), source="feed")
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["done"], 4)
        self.assertEqual(state["attempts"], 1)

        # 重入：再跑一次，done 不翻倍，别名无重复
        m.run_batch("b1", self._items(), source="feed")
        state2 = m.batch_state("b1")
        self.assertEqual(state2["attempts"], 2)
        self.assertEqual(state2["done"], 4)
        n_aliases = m.con.execute(
            "SELECT COUNT(*) FROM aliases").fetchone()[0]
        self.assertEqual(n_aliases, 5)  # 4 项 + supersede 的旧 DOI

    def test_interrupted_write_recovery_matches_clean_run(self):
        # 参考：一次性干净跑完的完整快照
        clean = dm.IdentifierMapper(":memory:")
        clean.run_batch("b1", self._items(), source="feed")
        clean_snap = clean.snapshot()

        # 中断恢复：先跑前 2 项并提交（模拟写入中断），再用同一批次续跑
        broken = dm.IdentifierMapper(":memory:")
        broken.begin_batch("b1", 4)
        for item in self._items()[:2]:
            broken.register(dm.normalize_identifier(item["raw"]),
                            source="feed", metadata=item.get("metadata"))
            broken.mark_done("b1")
        # 此时批次停在 running，进程被打断；新进程用同一 db 续跑
        broken.run_batch("b1", self._items(), source="feed")
        recovered_snap = broken.snapshot()

        self.assertEqual(recovered_snap["identifiers"], clean_snap["identifiers"])
        self.assertEqual(recovered_snap["aliases"], clean_snap["aliases"])
        self.assertEqual(recovered_snap["version_chain"],
                         clean_snap["version_chain"])
        # 批次终态一致；attempts 如实记录了“中断+续跑”的两段历史（可解释）
        for name in ("batch_id", "done", "total", "status", "last_error"):
            self.assertEqual(recovered_snap["batches"][0][name],
                             clean_snap["batches"][0][name])
        self.assertGreaterEqual(
            recovered_snap["batches"][0]["attempts"],
            clean_snap["batches"][0]["attempts"])

    def test_failed_batch_is_marked_and_retriable(self):
        m = dm.IdentifierMapper(":memory:")
        m.begin_batch("b2", 1)

        class Boom:
            def __str__(self):
                raise RuntimeError("simulated write failure")

        with self.assertRaises(Exception):
            m.run_batch("b2", [{"raw": Boom()}], source="feed")
        state = m.batch_state("b2")
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["last_error"])
        # 失败项未标记完成；修正输入后可用同批次续跑
        state2 = m.run_batch("b2", [{"raw": "10.1/retry"}], source="feed")
        self.assertEqual(state2["status"], "done")
        self.assertEqual(state2["done"], 1)

    def test_persistence_across_real_file_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alias.db")
            m1 = dm.IdentifierMapper(path)
            m1.register(dm.normalize_doi("10.1/file"), source="s")
            m1.close()
            m2 = dm.IdentifierMapper(path)  # 模拟迁移/重启后重开
            self.assertEqual(m2.resolve("10.1/file", "s"), "doi:10.1/file")
            m2.close()


class ReplayAndMigrationTests(unittest.TestCase):
    def _feed(self):
        return [
            {"id": "r1", "src": "crossref", "DOI": "10.1/A",
             "title": "T", "type": "journal-article"},
            {"id": "r2", "src": "crossref",
             "doi": "https://doi.org/10.1/a", "title": "T2"},
            {"id": "r3", "src": "arxiv", "arxiv_id": "2101.00001v1"},
            {"id": "r4", "src": "legacy",
             "url": "http://dx.doi.org/10.1/old", "dead_link": True},
            {"id": "r5", "src": "crossref", "doi": "10.1/new",
             "supersedes_raw": "10.1/old"},
        ]

    def _ingest(self, mapper):
        outs = []
        for rec in self._feed():
            out = dm.process(rec, mapper)
            if rec.get("supersedes_raw"):
                mapper.supersede(rec["supersedes_raw"], rec["doi"],
                                 source=rec["src"])
            outs.append(out)
        return outs

    def test_fresh_reentry_and_recovered_mapping_results_agree(self):
        # 1) 全新跑
        fresh = dm.IdentifierMapper(":memory:")
        self._ingest(fresh)
        fresh_snap = fresh.snapshot()

        # 2) 重入：同一 mapper 再喂一遍同样的输入
        self._ingest(fresh)
        reentry_snap = fresh.snapshot()
        self.assertEqual(reentry_snap, fresh_snap)  # 重放完全一致

        # 3) 迁移：落盘后在新连接上重放
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            file_mapper = dm.IdentifierMapper(path)
            self._ingest(file_mapper)
            file_mapper.close()
            reopened = dm.IdentifierMapper(path)
            self._ingest(reopened)  # 重放历史输入
            self.assertEqual(reopened.snapshot()["identifiers"],
                             fresh_snap["identifiers"])
            self.assertEqual(reopened.snapshot()["aliases"],
                             fresh_snap["aliases"])
            self.assertEqual(reopened.snapshot()["version_chain"],
                             fresh_snap["version_chain"])
            reopened.close()

    def test_historical_keys_remain_resolvable_after_upgrade(self):
        m = dm.IdentifierMapper(":memory:")
        self._ingest(m)
        # 旧 DOI 已被新 DOI 取代，但旧键仍可解析、可解释
        old_key = m.resolve("10.1/old", "legacy")
        self.assertIsNotNone(old_key)
        self.assertEqual(m.canonical_key(old_key), "doi:10.1/new")
        chain = m.version_chain(old_key)
        self.assertTrue(any(e["relation"] == "supersede" for e in chain))

    def test_two_dead_link_policies_remain_explainable_in_replay(self):
        explainable = {}
        for policy in (dm.DEAD_LEGACY, dm.DEAD_PENDING):
            m = dm.IdentifierMapper(":memory:", dead_link_policy=policy)
            out = dm.process({"id": "r4", "src": "legacy",
                              "url": "http://dx.doi.org/10.1/old",
                              "dead_link": True}, m)
            explainable[policy] = {
                "review": out["doi_review"],
                "alias_status": [a["status"] for a in
                                 m.con.execute("SELECT status FROM aliases")],
                "events": [e["event_type"] for e in m.events()],
            }
        self.assertEqual(explainable[dm.DEAD_LEGACY]["alias_status"], ["dead"])
        self.assertEqual(explainable[dm.DEAD_PENDING]["alias_status"],
                         ["pending"])
        self.assertIn("dead_alias", explainable[dm.DEAD_PENDING]["review"])
        self.assertNotIn("dead_alias", explainable[dm.DEAD_LEGACY]["review"])


class ConsumerAndCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.m = in_memory_mapper()

    def test_harvest_cursor_is_stable_across_writings(self):
        r1 = dm.process({"id": "a", "src": "s", "doi": "10.1/a"}, self.m)
        r2 = dm.process({"id": "a2", "src": "s",
                         "doi": "https://doi.org/10.1/A"}, self.m)
        h1 = harvest.process(r1)
        h2 = harvest.process(r2)
        self.assertEqual(h1["harvest_cursor"], h2["harvest_cursor"])
        self.assertEqual(harvest.cursor_of(r1, self.m), "doi:10.1/a")

    def test_harvest_falls_back_to_record_id_without_identity(self):
        rec = harvest.process({"id": "r-001", "src": "alpha"})
        self.assertEqual(rec["_stage_harvest"], "naive")
        self.assertEqual(harvest.cursor_of(rec), "r-001")

    def test_dedup_merges_same_work_and_keeps_baseline_marker(self):
        r1 = dm.process({"id": "a", "src": "crossref", "DOI": "10.1/a",
                         "arxiv_id": "2101.00001"}, self.m)
        d1 = dedup.process(r1)
        self.assertEqual(d1["dedup_action"], "merge")
        self.assertEqual(d1["_stage_dedup"], "ok")
        plain = dedup.process({"id": "r-002", "src": "beta"})
        self.assertEqual(plain["_stage_dedup"], "ok")
        self.assertNotIn("dedup_group", plain)
        r2 = dm.process({"id": "b", "src": "arxiv",
                         "arxiv_id": "2101.00001v5"}, self.m)
        self.assertTrue(dedup.same_work(r1, r2, self.m))

    def test_version_chain_helper_serves_version_stage(self):
        self.m.supersede("10.1/old", "10.1/new", source="crossref")
        rec = dm.process({"id": "v", "src": "crossref",
                          "doi": "10.1/old"}, self.m)
        chain = dm.version_chain_of(rec, self.m)
        self.assertTrue(chain)
        self.assertEqual(chain[0]["to_key"], "doi:10.1/new")

    def test_naive_baseline_behavior_for_records_without_identifiers(self):
        rec = dm.process({"id": "r-001", "src": "alpha",
                          "name": " Record One ", "value": 10})
        self.assertEqual(rec["_stage_doi_map"], "naive")
        self.assertNotIn("doi_key", rec)
        # 原始 id 不动，keyer 主键行为不变
        from papertrail.keyer import key_of
        self.assertEqual(key_of(rec), "r-001")

    def test_pipeline_compatible_with_fixture_records(self):
        # 夹具记录不含标识字段：整条流水线行为与基线一致
        from papertrail import pipeline
        result = pipeline.run()
        self.assertEqual(result["fed"], 4)
        self.assertEqual(result["rows"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
