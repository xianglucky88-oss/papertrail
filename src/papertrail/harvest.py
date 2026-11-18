"""单一职责：对记录做单一职责变换：harvest。

通用记录打 naive 标记；论文记录回填抓取来源，不做业务判断。
"""
def process(record, ctx=None):
    rec = dict(record)
    if "bylines" in rec:
        rec.setdefault("_stage_harvest", "paper")
        return rec
    rec.setdefault("_stage_harvest", "naive")
    return rec
