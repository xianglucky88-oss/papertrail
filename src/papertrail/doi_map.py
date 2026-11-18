"""单一职责：对记录做单一职责变换：doi_map。

论文记录建立 doi -> 记录 id 的映射（存入键值层），供 version 阶段查询。
"""
from . import store

DOI_PREFIX = "doi_map:"

def process(record, ctx=None):
    rec = dict(record)
    doi = rec.get("doi")
    if doi:
        if ctx is not None and ctx.con is not None:
            store.put(ctx.con, DOI_PREFIX + doi, {"record_id": rec.get("id"), "doi": doi})
        rec.setdefault("_stage_doi_map", "indexed")
    else:
        rec.setdefault("_stage_doi_map", "naive")
    return rec
