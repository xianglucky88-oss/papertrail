"""单一职责：论文作者消歧与署名规范。

输入：normalizer 产出的论文记录（含有序 bylines）。
输出：在记录上追加 author_resolution（按署名顺序逐行给出身份解析结果），
      作者身份实体与待人工队列通过现有键值层持久化。

设计要点
--------
1. ORCID 是硬锚点：ORCID 相同 => 同一人（自动确认）；ORCID 冲突 => 硬阻断，
   即使名字相同也禁止自动合并，转待人工。
2. 无 ORCID 时按「同名 + 同合作者」圈定候选，对每条候选汇总可回放证据，
   证据是命名信号与权重的列表，而不是单个相似度。
3. 两条显式门槛：
   - AUTO_CONFIRM_SCORE 及以上且无冲突 => 自动确认合并；
   - MANUAL_REVIEW_SCORE 及以下、或命中机构冲突护栏、或出现多候选 => 待人工。
   缺 ORCID 且机构冲突时护栏要求至少 2 个独立佐证锚点，否则一律不自动合并，
   以「宁漏合并、勿误合并」为默认。
4. 作者顺序（bylines[].order）属于论文内部事实，与作者身份分开存放；
   身份实体只保存它出现在哪些论文的哪个署名位。
5. 所有判定以稳定键幂等落库：重复导入不会改变既有身份，只会回放既有采用结果；
   待人工队列持久化，人工确认/中断恢复后可继续回放。
"""
import hashlib
from . import store

# ---- 键前缀（全部走通用键值层）-------------------------------------------
IDENTITY_PREFIX = "author_identity:"
REVIEW_PREFIX = "author_review:"
ANON_PREFIX = "author_anon:"

# ---- 门槛与证据权重（集中声明，便于审计与调参）---------------------------
AUTO_CONFIRM_SCORE = 0.75          # 达到即自动确认（还须通过冲突护栏）
MANUAL_REVIEW_SCORE = 0.45         # 低于此分视为证据不足，不自动合并
CONFLICT_GUARD_ANCHORS = 2         # 机构冲突护栏所需的独立佐证锚点数

W_ORCID_EXACT = 1.00
W_NAME_EXACT = 0.40
W_PREV_NAME = 0.45
W_NAME_TOKEN = 0.10
W_AFFIL_OVERLAP = 0.15
W_COAUTHOR_EACH = 0.20
W_COAUTHOR_CAP = 0.40
W_POSITION_SAME = 0.05


# ---- 纯函数：文本规整与候选打分 ------------------------------------------
def _name_tokens(name):
    return {t for t in "".join(c if c.isalnum() else " " for c in (name or "").lower()).split()}

def _name_match(name, identity):
    """返回 (最佳匹配权重, 证据标签)。"""
    target = _name_tokens(name)
    if name and name.lower() == identity["name"].lower():
        return W_NAME_EXACT, "name_exact"
    for prev in identity.get("previous_names", []):
        if name and name.lower() == prev.lower():
            return W_PREV_NAME, "previous_name"
    known = _name_tokens(identity["name"])
    for prev in identity.get("previous_names", []):
        known |= _name_tokens(prev)
    if target and target <= known:
        return W_NAME_TOKEN, "name_token_overlap"
    return 0.0, None

def score_candidate(byline, identity, paper_coauthors):
    """对单个候选身份汇总证据。返回 (score, evidence[], conflict)。

    evidence 是可回放的 (信号, 权重) 列表；conflict 为硬冲突标签列表。
    """
    evidence, conflicts = [], []
    score = 0.0

    b_orcid, i_orcid = byline.get("orcid", ""), identity.get("orcid", "")
    if b_orcid and i_orcid:
        if b_orcid == i_orcid:
            score += W_ORCID_EXACT
            evidence.append(("orcid_exact", W_ORCID_EXACT))
        else:
            conflicts.append("orcid_conflict")
            return 0.0, evidence, conflicts          # 硬阻断：同名也不许合并

    w, tag = _name_match(byline["name"], identity)
    if tag:
        score += w
        evidence.append((tag, w))

    b_aff = set(byline.get("affiliations", []))
    i_aff = set(identity.get("affiliations", []))
    if b_aff and i_aff:
        if b_aff & i_aff:
            score += W_AFFIL_OVERLAP
            evidence.append(("affiliation_overlap", W_AFFIL_OVERLAP))
        else:
            conflicts.append("affiliation_conflict")  # 不致命，但触发护栏

    shared = sorted(set(paper_coauthors) & set(identity.get("coauthors", [])))
    if shared:
        w = min(W_COAUTHOR_CAP, W_COAUTHOR_EACH * len(shared))
        score += w
        evidence.append(("shared_coauthors:%d" % len(shared), round(w, 4)))

    if byline.get("order") in identity.get("positions", []):
        score += W_POSITION_SAME
        evidence.append(("same_author_position", W_POSITION_SAME))

    return round(min(score, 1.0), 4), evidence, conflicts

def _corroboration_anchors(evidence):
    """独立佐证锚点：剔除名字类与顺序弱信号后，剩下的独立证据种类数。"""
    anchors = set()
    for tag, _w in evidence:
        if tag.startswith(("name_", "previous_name", "same_author_position")):
            continue
        anchors.add(tag.split(":", 1)[0])
    return len(anchors)


# ---- 有状态解析器：身份表 + 待人工队列，全部持久化 -----------------------
class AuthorResolver:
    def __init__(self, con):
        self.con = con
        self.identities = {k: v for k, v in store.scan(con, IDENTITY_PREFIX)}
        self.reviews = {k: v for k, v in store.scan(con, REVIEW_PREFIX)}
        self._seq = len(self.identities) + len(self.reviews)

    def _new_id(self, byline, paper_id):
        if byline.get("orcid"):
            return IDENTITY_PREFIX + "orcid:" + byline["orcid"]
        h = hashlib.sha256(
            ("%s|%s" % (paper_id, byline["order"])).encode()
        ).hexdigest()[:12]
        return IDENTITY_PREFIX + "lid:" + h

    def _save_identity(self, key, data):
        self.identities[key] = data
        store.put(self.con, key, data)

    def _save_review(self, key, data):
        self.reviews[key] = data
        store.put(self.con, key, data)

    # -- 候选圈定：同名（含曾用名）或同合作者，排除匿名身份 ----------------
    def _candidates(self, byline, paper_coauthors):
        names = {byline["name"].lower()} if byline["name"] else set()
        out = []
        for key, ident in self.identities.items():
            if ident.get("anonymous"):
                continue
            known = {ident["name"].lower(), *(p.lower() for p in ident.get("previous_names", []))}
            if names & known or (paper_coauthors and
                                 set(paper_coauthors) & set(ident.get("coauthors", []))):
                out.append((key, ident))
        return out

    def _adopt(self, key, byline, paper_id, status, evidence, score, decision):
        ident = self.identities[key]
        slot = {"paper_id": paper_id, "order": byline["order"]}
        if slot in ident["papers"]:
            return ident                       # 重复导入：既有身份保持不变
        ident["status"] = status
        if byline.get("orcid") and not ident.get("orcid"):
            ident["orcid"] = byline["orcid"]
        # 改名语义：署名行显式声明的曾用名入历史
        for nm in byline.get("previous_names", []):
            if nm and nm.lower() != ident["name"].lower() and nm not in ident.get("previous_names", []):
                ident.setdefault("previous_names", []).append(nm)
        for aff in byline.get("affiliations", []):
            if aff not in ident["affiliations"]:
                ident["affiliations"].append(aff)
        if byline.get("order") not in ident["positions"]:
            ident["positions"].append(byline["order"])
        if slot not in ident["papers"]:
            ident["papers"].append(slot)
        ident["evidence"].append({
            "seq": self._next_seq(), "paper_id": paper_id, "order": byline["order"],
            "decision": decision, "score": score, "signals": evidence,
        })
        ident["confidence"] = round(max(score, ident.get("confidence", 0.0)), 4)
        self._save_identity(key, ident)

    def _next_seq(self):
        self._seq += 1
        return self._seq

    def _open_review(self, byline, paper_id, candidate_key, scored, reason):
        rkey = REVIEW_PREFIX + hashlib.sha256(
            ("%s|%s" % (paper_id, byline["order"])).encode()).hexdigest()[:12]
        existing = self.reviews.get(rkey)
        if existing:                       # 重复导入：复用同一待审单，不新建身份
            return rkey, existing
        data = {
            "review_id": rkey, "paper_id": paper_id, "order": byline["order"],
            "byline": byline, "reason": reason,
            "candidate_identity": candidate_key,
            "candidates": [
                {"identity": k, "score": s, "evidence": ev, "conflicts": cf}
                for k, s, ev, cf in scored
            ],
            "status": "pending",
        }
        self._save_review(rkey, data)
        return rkey, data

    def _resolve_named(self, byline, paper_id, paper_coauthors):
        # 先看该署名位是否已挂起审核（中断恢复/重复导入 => 稳定复用）
        rkey = REVIEW_PREFIX + hashlib.sha256(
            ("%s|%s" % (paper_id, byline["order"])).encode()).hexdigest()[:12]
        pending = self.reviews.get(rkey)
        if pending and pending["status"] == "pending":
            return {"order": byline["order"], "status": "pending",
                    "author_id": None, "review_id": rkey,
                    "reason": pending["reason"], "evidence": [], "score": None}
        if pending and pending["status"] in ("merged", "separated"):
            return {"order": byline["order"], "status": "confirmed",
                    "author_id": pending["resolved_identity"],
                    "review_id": rkey, "reason": "manual_%s" % pending["status"],
                    "evidence": pending.get("resolution_evidence", []), "score": 1.0}

        scored = []
        for key, ident in self._candidates(byline, paper_coauthors):
            s, ev, cf = score_candidate(byline, ident, paper_coauthors)
            scored.append((key, s, ev, cf))
        scored.sort(key=lambda x: (-x[1], x[0]))

        # 硬冲突（ORCID 不一致）禁止自动合并，且优先于机构护栏报告
        hard = [x for x in scored if "orcid_conflict" in x[3]]
        if hard:
            rk, _ = self._open_review(byline, paper_id, hard[0][0], scored, "orcid_conflict")
            return {"order": byline["order"], "status": "pending", "author_id": None,
                    "review_id": rk, "reason": "orcid_conflict",
                    "evidence": hard[0][2], "score": hard[0][1]}

        if not scored:
            key = self._new_id(byline, paper_id)
            self._save_identity(key, self._fresh_identity(byline, paper_id))
            return self._confirmed(byline, key, [("new_identity_no_candidate", 0.0)], 0.0,
                                   "auto_new_identity")

        top_key, top_score, top_ev, top_cf = scored[0]
        tied = [x for x in scored if x[1] >= top_score]
        if len(tied) > 1:                  # 多候选同分 => 无法自动裁决
            rk, _ = self._open_review(byline, paper_id, top_key, scored, "ambiguous_candidates")
            return {"order": byline["order"], "status": "pending", "author_id": None,
                    "review_id": rk, "reason": "ambiguous_candidates",
                    "evidence": top_ev, "score": top_score}

        guarded = "affiliation_conflict" in top_cf
        anchors = _corroboration_anchors(top_ev)
        if guarded and anchors < CONFLICT_GUARD_ANCHORS:
            rk, _ = self._open_review(byline, paper_id, top_key, scored,
                                      "affiliation_conflict_guard")
            return {"order": byline["order"], "status": "pending", "author_id": None,
                    "review_id": rk, "reason": "affiliation_conflict_guard",
                    "evidence": top_ev, "score": top_score}

        if top_score >= AUTO_CONFIRM_SCORE:
            self._adopt(top_key, byline, paper_id, "confirmed", top_ev, top_score,
                        "auto_confirmed")
            return self._confirmed(byline, top_key, top_ev, top_score, "auto_confirmed")

        if top_score <= MANUAL_REVIEW_SCORE:
            rk, _ = self._open_review(byline, paper_id, top_key, scored, "insufficient_evidence")
            return {"order": byline["order"], "status": "pending", "author_id": None,
                    "review_id": rk, "reason": "insufficient_evidence",
                    "evidence": top_ev, "score": top_score}

        rk, _ = self._open_review(byline, paper_id, top_key, scored, "below_auto_threshold")
        return {"order": byline["order"], "status": "pending", "author_id": None,
                "review_id": rk, "reason": "below_auto_threshold",
                "evidence": top_ev, "score": top_score}

    def _fresh_identity(self, byline, paper_id):
        return {
            "identity_id": self._new_id(byline, paper_id),
            "orcid": byline.get("orcid", ""), "name": byline["name"],
            "previous_names": list(byline.get("previous_names", [])),
            "affiliations": list(byline.get("affiliations", [])),
            "positions": [byline["order"]],
            "papers": [{"paper_id": paper_id, "order": byline["order"]}],
            "coauthors": [], "anonymous": False, "status": "confirmed",
            "confidence": 0.0, "evidence": [{
                "seq": self._next_seq(), "paper_id": paper_id, "order": byline["order"],
                "decision": "auto_new_identity", "score": 0.0, "signals": [],
            }],
        }

    def _confirmed(self, byline, key, evidence, score, decision):
        return {"order": byline["order"], "status": "confirmed", "author_id": key,
                "review_id": None, "reason": decision,
                "evidence": evidence, "score": score}

    def _resolve_anonymous(self, byline, paper_id):
        key = ANON_PREFIX + hashlib.sha256(
            ("%s|%s" % (paper_id, byline["order"])).encode()).hexdigest()[:12]
        if key not in self.identities:      # 匿名身份按论文署名位隔离，绝不跨论文合并
            self._save_identity(key, {
                "identity_id": key, "orcid": "", "name": "", "previous_names": [],
                "affiliations": list(byline.get("affiliations", [])),
                "positions": [byline["order"]],
                "papers": [{"paper_id": paper_id, "order": byline["order"]}],
                "coauthors": [], "anonymous": True, "status": "anonymous",
                "confidence": 0.0, "evidence": [{
                    "seq": self._next_seq(), "paper_id": paper_id,
                    "order": byline["order"], "decision": "anonymous_slot",
                    "score": 0.0, "signals": [("anonymous_byline", 0.0)],
                }],
            })
        return {"order": byline["order"], "status": "anonymous", "author_id": key,
                "review_id": None, "reason": "anonymous_byline",
                "evidence": [("anonymous_byline", 0.0)], "score": 0.0}

    def resolve_record(self, record):
        paper_id = record.get("id") or "paper:" + hashlib.sha256(
            (record.get("doi") or record.get("title", "")).encode()
        ).hexdigest()[:12]
        bylines = record["bylines"]
        resolutions = [None] * len(bylines)

        # Pass 1：ORCID 锚定与匿名位，先建立/命中一批确定身份
        pass1_ids = []
        for i, bl in enumerate(bylines):
            if bl.get("anonymous"):
                resolutions[i] = self._resolve_anonymous(bl, paper_id)
            elif bl.get("orcid"):
                resolutions[i] = self._resolve_orcid(bl, paper_id)

        # Pass 2：无 ORCID 署名行，合作者集合此时已包含 pass1 的确定身份
        pass1_ids = [r["author_id"] for r in resolutions if r and r.get("author_id")]
        for i, bl in enumerate(bylines):
            if resolutions[i] is not None:
                continue
            resolutions[i] = self._resolve_named(bl, paper_id, pass1_ids)

        # 把本论文确认的合作者关系回写到各身份（无向共同作者图）
        confirmed = [r["author_id"] for r in resolutions if r.get("author_id")
                     and not self.identities[r["author_id"]].get("anonymous")]
        for aid in confirmed:
            ident = self.identities[aid]
            for other in confirmed:
                if other != aid and other not in ident["coauthors"]:
                    ident["coauthors"].append(other)
            self._save_identity(aid, ident)

        rec = dict(record)
        rec["author_resolution"] = resolutions
        rec["_stage_author_id"] = "ok"
        return rec

    def _resolve_orcid(self, byline, paper_id):
        key = IDENTITY_PREFIX + "orcid:" + byline["orcid"]
        if key not in self.identities:
            # 新建 ORCID 身份前，若已存在「带不同 ORCID」的同名身份，
            # 说明来源元数据可能张冠李戴：不自动合并，转待人工。
            # 无 ORCID 的同名身份不算硬冲突，留给证据打分/机构护栏处理。
            clash = self._same_name_other_orcid_identity(byline, exclude=key)
            if clash is not None:
                rk, _ = self._open_review(byline, paper_id, clash,
                    [{"identity": clash, "score": 0.0, "evidence": [],
                      "conflicts": ["orcid_conflict"]}], "orcid_conflict")
                return {"order": byline["order"], "status": "pending", "author_id": None,
                        "review_id": rk, "reason": "orcid_conflict",
                        "evidence": [("distinct_orcid_for_same_name", 0.0)], "score": 0.0}
            self._save_identity(key, self._fresh_identity(byline, paper_id))
            return self._confirmed(byline, key, [("orcid_exact", W_ORCID_EXACT)], 1.0,
                                   "auto_confirmed_orcid")
        ident = self.identities[key]
        slot = {"paper_id": paper_id, "order": byline["order"]}
        if slot in ident["papers"]:           # 重复导入：直接回放既有采用结果
            return self._confirmed(byline, key,
                                   ident["evidence"][-1]["signals"] or [("orcid_exact", W_ORCID_EXACT)],
                                   1.0, "replayed_existing")
        # 已存在 ORCID 身份但与另一带不同 ORCID 的同名身份冲突：同样转待人工
        clash = self._same_name_other_orcid_identity(byline, exclude=key)
        if clash is not None:
            rk, _ = self._open_review(byline, paper_id, clash,
                [{"identity": clash, "score": 0.0, "evidence": [],
                  "conflicts": ["orcid_conflict"]}], "orcid_conflict")
            return {"order": byline["order"], "status": "pending", "author_id": None,
                    "review_id": rk, "reason": "orcid_conflict",
                    "evidence": [("distinct_orcid_for_same_name", 0.0)], "score": 0.0}
        ev = [("orcid_exact", W_ORCID_EXACT)]
        # 同 ORCID 改名：旧名进历史，当前名更新为最新署名（同一人，自动确认）
        if byline["name"] and ident["name"] and \
                byline["name"].lower() != ident["name"].lower():
            if ident["name"] not in ident.get("previous_names", []):
                ident.setdefault("previous_names", []).append(ident["name"])
            ident["name"] = byline["name"]
            ev.append(("recorded_name_change", 0.0))
        self._adopt(key, byline, paper_id, "confirmed", ev, 1.0, "auto_confirmed_orcid")
        return self._confirmed(byline, key, ev, 1.0, "auto_confirmed_orcid")

    def _same_name_other_orcid_identity(self, byline, exclude):
        """同名（含曾用名）、身份键不同且自身带 ORCID 的具名身份；没有则 None。

        无 ORCID 的本地身份不视为 ORCID 硬冲突，避免把后补的权威标识误拦。
        """
        if not byline.get("name"):
            return None
        wanted = byline["name"].lower()
        for k, ident in self.identities.items():
            if k == exclude or ident.get("anonymous") or not ident.get("orcid"):
                continue
            known = {ident["name"].lower(), *(p.lower() for p in ident.get("previous_names", []))}
            if wanted in known:
                return k
        return None

    # -- 人工确认：merge / separate，幂等，回放后下游可重算 ----------------
    def confirm(self, review_id, action, target_identity=None):
        review = self.reviews.get(review_id)
        if not review:
            raise KeyError(review_id)
        if review["status"] in ("merged", "separated"):
            return review                      # 已处理：重复确认不改变既有身份
        bl = review["byline"]
        paper_id = review["paper_id"]
        if action == "merge":
            target = target_identity or review.get("candidate_identity")
            if not target or target not in self.identities:
                raise ValueError("unknown target identity: %r" % target)
            self._adopt(target, bl, paper_id, "confirmed",
                        [("manual_merge", 1.0)], 1.0, "manual_merge")
            resolved = target
        elif action == "separate":
            key = self._new_id(bl, paper_id)
            self._save_identity(key, self._fresh_identity(bl, paper_id))
            resolved = key
        else:
            raise ValueError("action must be 'merge' or 'separate'")
        review["status"] = action + "d" if action == "separate" else "merged"
        review["resolved_identity"] = resolved
        review["resolution_evidence"] = [("manual_%s" % action, 1.0)]
        self._save_review(review_id, review)
        return review


# ---- 模块级阶段入口：与其它阶段签名一致（record, ctx）--------------------
def process(record, ctx=None):
    if "bylines" not in record:
        rec = dict(record)
        rec.setdefault("_stage_author_id", "skipped")
        return rec
    if ctx is None or ctx.con is None:
        raise RuntimeError("author_id.process requires a Context with a sqlite connection")
    resolver = AuthorResolver(ctx.con)
    return resolver.resolve_record(record)

def confirm_review(con, review_id, action, target_identity=None):
    review = AuthorResolver(con).confirm(review_id, action, target_identity)
    store.commit(con)
    return review

def pending_reviews(con):
    return [v for _k, v in store.scan(con, REVIEW_PREFIX) if v["status"] == "pending"]

def identities(con):
    return [v for _k, v in store.scan(con, IDENTITY_PREFIX)]
