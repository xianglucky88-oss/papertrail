"""单一职责：dedup 合并判断阶段 —— 基于 doi_map 的身份分组做同工作判定。

流水线阶段函数保持朴素兼容：无 ``doi_work_key`` 的记录只补 ``_stage_dedup``
标记，不产生合并组，基线 happy-path 的行数行为不变。

带身份的记录附加：
  * ``dedup_group``  —— 同工作合并组键（DOI/arXiv/旧 DOI 通过版本链与
    preprint_of 关系归并），组内任写一条代表键即可做合并判断；
  * ``dedup_action`` —— ``merge``（属于某个多成员组）或 ``unique``。

注意：失效别名在 doi_map 阶段已按策略落为旧键或待审映射，这里不做字段覆盖，
只读取已经可解释的身份事实，保证重放结果一致。
"""
from . import doi_map


def same_work(record_a, record_b, mapper=None):
    """两条记录是否属于同一学术工作（供合并判断）。"""
    ka = record_a.get("doi_work_key") or record_a.get("doi_key")
    kb = record_b.get("doi_work_key") or record_b.get("doi_key")
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    m = mapper or doi_map.get_mapper()
    return kb in m.same_work_keys(ka)


def process(record):
    rec = dict(record)
    if "doi_work_key" in rec:
        group = rec.get("doi_same_work") or [rec["doi_work_key"]]
        rec["dedup_group"] = "|".join(group)
        rec["dedup_action"] = "merge" if len(group) > 1 else "unique"
    rec.setdefault("_stage_dedup", "ok")
    return rec
