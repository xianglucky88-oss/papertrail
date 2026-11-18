"""单一职责：重复判定（作为流水线阶段）。

普通记录保持既有朴素行为；学术记录复用 version 子系统的身份与角色判定：
* 已索引为 duplicate 的同源记录 -> duplicate；
* 同一 paper_id 且同 kind/version_label/date 的节点 -> duplicate；
* 仅有弱证据（候选）的记录绝不判重，保留为不同论文。
"""
from . import version


def process(record):
    rec = dict(record)
    if not version.is_scholarly(rec):
        rec.setdefault("_stage_dedup", "ok")
        return rec

    con = version._SESSION_CON.get()
    role = rec.get("_version_role")
    if con is None:
        rec["_stage_dedup"] = "degraded_identity_only"
        rec["_is_duplicate"] = role == "duplicate"
        return rec

    record_key = version.key_of(rec)
    is_dup = version.is_duplicate_record(con, record_key) or role == "duplicate"
    rec["_is_duplicate"] = is_dup
    # 重新解析以覆盖同批次内改名/并链
    rec["_dedup_paper_id"] = version.paper_for_record(con, record_key)
    rec["_stage_dedup"] = "duplicate" if is_dup else "unique"
    return rec
