"""单一职责：预印本/正式发表版本归并（版本链）。

版本链的判定必须消费 author_id 的采用结果：作者身份集合是版本归并的核心证据，
同一作者的合并会改变版本链，因此这里复用与作者消歧相同的「证据 + 门槛」模式，
而不是返回一个相似度。

证据信号与门槛
--------------
- doi_link（记录显式声明 version_of / 命中 doi_map）  1.00
- 标题高度相似                                        0.30
- 规范化作者集合相交（按 canonical author_id）        0.40
- 发表时间晚于候选（预印本 -> 正式发表的方向性）      0.10

AUTO_CHAIN_SCORE=0.75：显式 DOI 链接单独即可成链；无显式链接时需要
标题 + 作者 + 时序共同达标。未达门槛只记录候选，等待后续导入或人工处理。
版本链以稳定键持久化，重复导入幂等、可回放。
"""
import hashlib
from . import store

CHAIN_PREFIX = "version_chain:"

AUTO_CHAIN_SCORE = 0.75
W_DOI_LINK = 1.00
W_TITLE = 0.30
W_AUTHORS = 0.40
W_TIME_ORDER = 0.10


def _title_tokens(title):
    return {t for t in "".join(c if c.isalnum() else " " for c in (title or "").lower()).split()}

def _canonical_authors(record):
    return sorted(r["author_id"] for r in record.get("author_resolution", [])
                  if r.get("author_id"))

def title_similarity(a, b):
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def author_overlap(a_ids, b_ids):
    sa, sb = set(a_ids), set(b_ids)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def evaluate(current, candidate):
    """对一条候选父记录汇总版本证据，返回 (score, evidence)。"""
    evidence = []
    score = 0.0
    if current.get("version_of") and current["version_of"] == candidate.get("doi"):
        score += W_DOI_LINK
        evidence.append(("doi_version_link", W_DOI_LINK))
    ts = title_similarity(current.get("title", ""), candidate.get("title", ""))
    if ts >= 0.6:
        score += W_TITLE
        evidence.append(("title_similarity:%.2f" % ts, W_TITLE))
    ao = author_overlap(_canonical_authors(current), _canonical_authors(candidate))
    if ao >= 0.5:
        score += W_AUTHORS
        evidence.append(("canonical_author_overlap:%.2f" % ao, W_AUTHORS))
    if current.get("ts", "") > candidate.get("ts", ""):
        score += W_TIME_ORDER
        evidence.append(("later_publication", W_TIME_ORDER))
    return round(min(score, 1.0), 4), evidence

def _chain_id(doi):
    return CHAIN_PREFIX + (doi or hashlib.sha256(b"paper").hexdigest()[:12])

def _index_records(con):
    return [v for _k, v in store.scan(con, "record:")]

def process(record, ctx=None):
    rec = dict(record)
    if "bylines" not in record:
        rec.setdefault("_stage_version", "skipped")
        return rec
    if ctx is None or ctx.con is None:
        raise RuntimeError("version.process requires a Context with a sqlite connection")

    # 候选发现：显式 version_of 指向的记录优先，其次取标题/作者相近的既有记录
    candidates = []
    for other in _index_records(ctx.con):
        if other.get("id") == record.get("id"):
            continue
        if record.get("version_of") and record["version_of"] == other.get("doi"):
            candidates.insert(0, other)
        elif title_similarity(record.get("title", ""), other.get("title", "")) >= 0.6:
            candidates.append(other)

    best, best_score, best_ev = None, 0.0, []
    scored_all = []
    for cand in candidates:
        s, ev = evaluate(record, cand)
        scored_all.append({"record_id": cand.get("id"), "score": s, "evidence": ev})
        if s > best_score:
            best, best_score, best_ev = cand, s, ev

    if best is not None and best_score >= AUTO_CHAIN_SCORE:
        parent_chain = best.get("version", {}).get("chain_id") or _chain_id(best.get("doi"))
        chain_id = parent_chain
        rec["version"] = {
            "chain_id": chain_id, "parent_id": best.get("id"),
            "status": "chained", "score": best_score, "evidence": best_ev,
        }
        chain = store.get(ctx.con, chain_id, {"chain_id": chain_id, "members": []})
        members = chain["members"]
        for mid in (best.get("id"), record.get("id")):
            if mid and mid not in members:
                members.append(mid)
        chain["members"] = members
        store.put(ctx.con, chain_id, chain)
    else:
        rec["version"] = {
            "chain_id": _chain_id(record.get("doi")), "parent_id": None,
            "status": "chain_head" if not scored_all else "pending_link",
            "score": best_score, "evidence": best_ev, "candidates": scored_all,
        }
    rec["_stage_version"] = "ok"
    # 维护本阶段所需的既有记录索引（幂等 upsert，重跑不增行）
    if rec.get("id"):
        store.put(ctx.con, "record:" + str(rec["id"]), rec)
    return rec
