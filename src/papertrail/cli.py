import json, sys
from . import pipeline, store, author_id


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "generic"
    if kind == "papers":
        from .config import PAPER_FIXTURES
        con = store.connect()
        try:
            summary = pipeline.run(con, path=PAPER_FIXTURES, kind="papers")
            summary["pending_reviews"] = len(author_id.pending_reviews(con))
            summary["identities"] = len(author_id.identities(con))
        finally:
            con.close()
        print(json.dumps(summary, ensure_ascii=False))
        return
    print(json.dumps(pipeline.run(), ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
