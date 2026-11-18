"""业务阶段：doi_map 规范键 / version 版本状态 / dedup 去重 / keyer 主键。"""
import pytest

from src.papertrail import dedup, doi_map, keyer, version


class TestDoiMap:
    @pytest.mark.parametrize("raw", [
        "10.1000/ABC",
        "https://doi.org/10.1000/abc",
        "http://dx.doi.org/10.1000/Abc",
        "doi:10.1000/abc",
        "DOI: 10.1000/abc",
        " info:doi/10.1000/abc ",
        "10.1000/abc.",
    ])
    def test_canonical_doi_variants(self, raw):
        assert doi_map.canonical_doi(raw) == "10.1000/abc"

    @pytest.mark.parametrize("raw", [None, "", "not-a-doi", "10.1/too-short", "abc"])
    def test_invalid_doi(self, raw):
        assert doi_map.canonical_doi(raw) is None

    @pytest.mark.parametrize("field", ["doi", "DOI", "Doi"])
    def test_field_drift(self, field):
        assert doi_map.canonical_key({field: "10.1000/X"}) == "doi:10.1000/x"

    def test_process_sets_canonical_key(self):
        out = doi_map.process({"doi": "https://doi.org/10.1000/X"})
        assert out["_canonical_key"] == "doi:10.1000/x"
        assert out["_doi"] == "10.1000/x"

    def test_process_is_deterministic(self):
        a = doi_map.process({"doi": "10.1000/X"})
        b = doi_map.process({"doi": "10.1000/X"})
        assert a == b


class TestVersion:
    def test_published_beats_preprint(self):
        pub = version.process({"type": "journal-article"})
        pre = version.process({"type": "preprint"})
        assert pub["_version_rank"] > pre["_version_rank"]

    @pytest.mark.parametrize("record,expected", [
        ({"type": "journal-article"}, "published"),
        ({"publication_type": "posted-content"}, "preprint"),
        ({"status": "accepted"}, "published"),
        ({"published": True}, "published"),
        ({"is_preprint": True}, "preprint"),
        ({}, "unknown"),
    ])
    def test_classify_drift(self, record, expected):
        assert version.classify(record)[0] == expected

    def test_merge_refuses_downgrade(self):
        old = {"_version": "published", "_version_rank": 3, "v": 1}
        new = {"_version": "preprint", "_version_rank": 2, "v": 2}
        assert version.merge(old, new) is old

    def test_merge_allows_upgrade(self):
        old = {"_version": "preprint", "_version_rank": 2}
        new = {"_version": "published", "_version_rank": 3}
        assert version.merge(old, new) is new


class TestDedup:
    def test_doi_collapses_variants(self):
        a = dedup.process({"doi": "10.1000/X"})
        b = dedup.process({"DOI": "https://doi.org/10.1000/x"})
        assert a["_dedup_key"] == b["_dedup_key"] == "doi:10.1000/x"

    def test_falls_back_to_id(self):
        assert dedup.dedup_key({"id": "r-1"}) == "id:r-1"

    def test_batch_dedup_keeps_last(self):
        records = [dedup.process({"id": "a", "v": 1}),
                   dedup.process({"id": "a", "v": 2})]
        unique, dups = dedup.dedup_records(records)
        assert len(unique) == 1
        assert unique[0]["v"] == 2
        assert dups == 1


class TestKeyer:
    def test_canonical_key_wins(self):
        rec = doi_map.process({"id": "x", "doi": "10.1000/Y"})
        assert keyer.key_of(rec) == "doi:10.1000/y"

    def test_id_preserved_for_legacy(self):
        assert keyer.key_of({"id": "r-001"}) == "r-001"

    def test_deterministic_without_id(self):
        a = keyer.key_of({"name": "x"})
        b = keyer.key_of({"name": "x"})
        assert a == b
