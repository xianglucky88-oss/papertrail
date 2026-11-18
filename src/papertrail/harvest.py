"""单一职责：harvest 抓取阶段 —— 增量游标基于 doi_map 的稳定身份键。

流水线阶段函数保持朴素兼容：无标识字段的记录只补 ``_stage_harvest`` 标记。
带 ``doi_key`` 的记录额外获得：
  * ``harvest_cursor`` —— 身份当前代表键（沿别名升级链走到终点），作为增量
    游标语义：同一工作的历史写法（URL/大小写/旧 DOI）都映射到同一游标位置，
    重放时不会因为写法变化而重复抓取或跳过。
"""
from . import doi_map


def cursor_of(record, mapper=None):
    """供增量抓取使用的稳定游标值；无身份记录回退到记录自身 id。"""
    key = doi_map.canonical_key_of(record, mapper)
    if key:
        return key
    rid = record.get("id")
    return str(rid) if rid else None


def process(record):
    rec = dict(record)
    if "doi_key" in rec:
        rec["harvest_cursor"] = doi_map.canonical_key_of(rec)
    rec.setdefault("_stage_harvest", "naive")
    return rec
