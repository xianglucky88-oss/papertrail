"""单一职责：论文去重（流水线阶段）。

去重键不再只依赖输入 id，而是消费 author_id 的采用结果与 version 的版本链：
    dedup_fingerprint = sha256( doi | 标题 token 集 | 有序 canonical author_id 元组 | 版本链 )

因此「同一作者的合并会改变去重键」——身份采用是可回放的，键也就是可回放的，
而不是一个瞬时相似度。署名顺序变化不影响键（身份集合才是身份事实），
但同名不同人因 author_id 不同必然得到不同的键。

若任一署名位仍是 pending（待人工），去重进入 guarded：给出独立指纹但不声明
与任何记录重复，避免在身份未定时误折叠。
"""
import hashlib
from . import store

DEDUP_PREFIX = "dedup:"

def _fingerprint(record):
    authors = tuple(sorted(
        r["author_id"] for r in record.get("author_resolution", []) if r.get("author_id")
    ))
    title_tokens = " ".join(sorted(
        "".join(c if c.isalnum() else " " for c in (record.get("title") or "").lower()).split()
    ))
    chain = record.get("version", {}).get("chain_id", "")
    basis = "|".join([record.get("doi", ""), title_tokens, ",".join(authors), chain])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]

def process(record, ctx=None):
    rec = dict(record)
    if "bylines" not in record:
        rec.setdefault("_stage_dedup", "ok")
        return rec

    pending = [r["order"] for r in record.get("author_resolution", [])
               if r.get("status") == "pending"]
    fp = _fingerprint(record)
    dedup_key = DEDUP_PREFIX + fp

    if pending:
        rec["dedup"] = {
            "key": dedup_key, "status": "guarded",
            "reason": "pending_author_review", "pending_orders": pending,
            "evidence": [("unresolved_byline_order:%s" % o, 0.0) for o in pending],
        }
    else:
        seen = store.get(ctx.con, dedup_key) if ctx and ctx.con else None
        rec["dedup"] = {
            "key": dedup_key,
            "status": "duplicate" if seen else "unique",
            "reason": "fingerprint_match" if seen else "new_fingerprint",
            "evidence": [("canonical_author_fingerprint", 1.0)],
        }
        if ctx and ctx.con:
            entry = seen or {"key": dedup_key, "record_ids": []}
            entry.setdefault("record_ids", [])
            if record.get("id") and record["id"] not in entry["record_ids"]:
                entry["record_ids"].append(record["id"])
            store.put(ctx.con, dedup_key, entry)
    rec["_stage_dedup"] = "ok"
    return rec
