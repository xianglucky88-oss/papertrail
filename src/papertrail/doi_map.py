"""单一职责：主标识（canonical identity）映射。

普通流水线记录保持既有的朴素行为；学术记录通过 version 子系统解析主标识：
同一论文的预印本与正式版共享同一个 paper_id（主标识），DOI 解析优先指向
该 paper；version.session 不可用时退化为本地 DOI 归一，不隐式建库。
"""
import re

from . import version


def _norm_doi(value):
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    return re.sub(r"^doi:", "", v)


def process(record):
    rec = dict(record)
    if not version.is_scholarly(rec):
        rec.setdefault("_stage_doi_map", "naive")
        return rec

    con = version._SESSION_CON.get()
    if con is None:
        paper_id = rec.get("_version_paper_id")
        rec["_stage_doi_map"] = "degraded_local_doi"
        rec["_canonical_paper_id"] = paper_id or ("doi:" + _norm_doi(rec.get("doi")))
        return rec

    # 始终按记录索引重新解析：同批次内后到的正式稿可能已把 paper 改名/并链
    paper_id = version.paper_for_record(con, version.key_of(rec))
    rec["_canonical_paper_id"] = paper_id
    if paper_id is not None:
        formal = version.current_formal(con, paper_id)
        basis = version.current_formal_basis(con, paper_id)
        if formal is not None:
            # 主标识解析到“当前正式版”：先到的正式稿不会因为到达顺序丢失
            rec["_canonical_doi"] = formal["metadata"].get("doi", "")
            rec["_canonical_formal_node_id"] = formal["node_id"]
            rec["_canonical_formal_basis_rule"] = (basis or {}).get("rule")
        else:
            rec.setdefault("_canonical_doi", _norm_doi(rec.get("doi")))
        rec["_stage_doi_map"] = "version_anchored"
    else:
        rec["_stage_doi_map"] = "unresolved"
    return rec
