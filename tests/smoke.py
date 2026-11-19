"""零依赖冒烟测试：环境没有 pytest 时可用 `python3 tests/smoke.py` 运行。

与 tests/ 下的 pytest 用例覆盖同一批场景；pytest 可用时优先 `make test`。
"""
import json
import sys
import tempfile
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from papertrail import author_id, store, version, normalizer, pipeline  # noqa: E402
from papertrail.models import Context  # noqa: E402

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "papers.json"
_passed = 0


def check(name, cond):
    global _passed
    assert cond, name
    _passed += 1
    print("PASS", name)


def stage(ctx, rec, names):
    for n in names:
        mod = __import__("papertrail." + n, fromlist=["process"])
        rec = (mod.process(rec, ctx)
               if len(inspect.signature(mod.process).parameters) >= 2
               else mod.process(rec))
    return rec


def aid(ctx, rec):
    return stage(ctx, rec, ["normalizer", "harvest", "author_id"])


def full(ctx, rec):
    return stage(ctx, rec, ["normalizer", "harvest", "author_id",
                            "doi_map", "version", "dedup"])


def new_ctx():
    return Context(con=store.connect(Path(tempfile.mkdtemp()) / "smoke.db"))


def main():
    raws = json.loads(FIX.read_text(encoding="utf-8"))

    check("orcid valid",
          normalizer.normalize_orcid("https://orcid.org/0000-0002-1825-0097")
          == "0000-0002-1825-0097")
    check("orcid bad checksum",
          normalizer.normalize_orcid("0000-0002-1825-0098") == "")

    ctx = new_ctx()
    r1 = aid(ctx, raws[0])
    r3 = aid(ctx, raws[2])
    wz1, wz3 = (r1["author_resolution"][0]["author_id"],
                r3["author_resolution"][0]["author_id"])
    check("homonym distinct", wz1 != wz3)

    r4 = aid(ctx, raws[3])
    ana = r4["author_resolution"][0]["author_id"]
    check("rename same identity",
          ana == r1["author_resolution"][1]["author_id"])
    ident = store.get(ctx.con, ana)
    check("rename history",
          ident["name"] == "Carla Mendes" and "Ana Torres" in ident["previous_names"])

    r5 = aid(ctx, raws[4])
    check("conflict goes manual",
          r5["author_resolution"][0]["status"] == "pending")
    review_id = r5["author_resolution"][0]["review_id"]
    merged = author_id.confirm_review(ctx.con, review_id, "merge",
                                      target_identity=wz1)
    check("manual merge", merged["resolved_identity"] == wz1)
    replay = aid(ctx, raws[4])
    check("replay converges",
          replay["author_resolution"][0]["author_id"] == wz1)

    r6 = aid(ctx, raws[5])
    check("anonymous isolated",
          r6["author_resolution"][0]["status"] == "anonymous")

    before = json.dumps(store.get(ctx.con, wz1), sort_keys=True)
    aid(ctx, raws[6])
    check("reimport stable",
          json.dumps(store.get(ctx.con, wz1), sort_keys=True) == before)

    r2 = aid(ctx, raws[1])
    check("reorder keeps identities",
          {x["author_id"] for x in r2["author_resolution"]}
          == {x["author_id"] for x in r1["author_resolution"]})

    ctxc = new_ctx()
    f1, f2, f3, f5 = (full(ctxc, raws[0]), full(ctxc, raws[1]),
                      full(ctxc, raws[2]), full(ctxc, raws[4]))
    check("version chain",
          f2["version"]["chain_id"] == f1["version"]["chain_id"]
          and f2["version"]["status"] == "chained")
    check("dedup distinct for homonyms", f1["dedup"]["key"] != f3["dedup"]["key"])
    check("guarded dedup", f5["dedup"]["status"] == "guarded")

    db = Path(tempfile.mkdtemp()) / "p.db"
    a = pipeline.run(store.connect(db), path=FIX, kind="papers")
    b = pipeline.run(store.connect(db), path=FIX, kind="papers")
    check("pipeline idempotent", a == b and a["fed"] == 7)

    db2 = Path(tempfile.mkdtemp()) / "g.db"
    g = pipeline.run(store.connect(db2))
    check("legacy pipeline", g == {"fed": 4, "rows": 3})

    print("\nsmoke: %d checks passed" % _passed)


if __name__ == "__main__":
    main()
