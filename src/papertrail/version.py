"""单一职责：版本归并（预印本 / 正式版 / 撤回）。

本模块在“论文身份（paper）”之下保存版本节点（node）、节点前后关系（edge）
与正式版选择依据（formal_basis），并把每个决定以只追加事件写入 ``vx_events``，
使全部物化状态都可以从历史重算（``rebuild``）。

归并规则（证据由强到弱；强证据自动接链，弱证据只保留候选、暂不归并）
------------------------------------------------------------------
强证据（自动并入同一条版本链）：
  R1_DOI           两条记录拥有相同的非空 DOI（DOI 为权威标识；标题/作者冲突
                   仍归并，但另记 metadata_conflict，原始元数据互不覆盖）。
  R2_ARXIV         两条记录拥有相同的非空 arXiv id。
  R3_TITLE_AUTHORS 标题指纹相同、作者指纹集合完全一致（至少一名作者），且不存在
                   冲突的 DOI / arXiv 标识。日期冲突不阻断本规则，只记冲突事件。

弱证据（写入 vx_candidates，保留双方各自的链，等待桥接证据）：
  W1_TITLE_AUTHOR  标题指纹相同且至少一名共有作者，但 DOI/arXiv 标识相互冲突，
                   或双方都缺少可锚定的标识。
  W2_TITLE_ONLY    仅标题指纹相同，缺少共有作者佐证。
  W3_IDENT_CONFLICT 标题/作者相似，但 DOI 或 arXiv 标识明确不同（身份存疑）。

桥接：一条同时携带两条链标识的新记录触发 paper_merged，旧链节点整体并入目标
链，相关候选标记为已解决；目标 paper_id 按 DOI > arXiv > 指纹的锚定优先级确定。

正式版（VOR）选择规则（只看证据，不看到达先后；同票按内容哈希确定性打破）：
  1. 仅在 kind='published' 节点中选择；没有 published 节点时正式版为空，
     当前版本回退为最新预印本，依据中写明 reason='no_published_version'。
  2. 按 (node_date 降序, version_label 降序, node_id 升序) 取首个节点。
  3. 被撤回不改变正式版归属：节点仍为正式版，但 formal_basis 标注 retracted
     并关联撤回事实（vx_retractions 可逐条找回）。
  4. 每次选择写入 formal_selected 事件并保存完整依据；物化表只保留最新一次，
     历史选择与依据在事件流中永久保留，供规则变更后历史重算与比对。
"""
import contextvars
import hashlib
import json
import re
from datetime import datetime, timezone

from . import store
from .keyer import key_of

# --- 规则常量（候选 reason / 事件 kind 统一引用，避免魔法字符串） ------------

RULE_DOI = "R1_DOI"
RULE_ARXIV = "R2_ARXIV"
RULE_TITLE_AUTHORS = "R3_TITLE_AUTHORS"
CANDIDATE_TITLE_AUTHOR = "W1_TITLE_AUTHOR"
CANDIDATE_TITLE_ONLY = "W2_TITLE_ONLY"
CANDIDATE_IDENT_CONFLICT = "W3_IDENT_CONFLICT"

#: 对外可枚举的候选保留规则说明（测试与文档引用同一份事实）。
CANDIDATE_RULES = {
    CANDIDATE_TITLE_AUTHOR: "标题相同且有共有作者，但标识冲突或缺失，证据不足以自动接链",
    CANDIDATE_TITLE_ONLY: "仅标题相同，无共有作者佐证，保留候选待桥接证据",
    CANDIDATE_IDENT_CONFLICT: "标题/作者相似但 DOI 或 arXiv 标识明确不同，身份存疑",
}

KIND_PREPRINT = "preprint"
KIND_PUBLISHED = "published"
KIND_RETRACTION = "retraction"

_SESSION_CON = contextvars.ContextVar("vx_session_con", default=None)
_SESSION_TOUCHED = contextvars.ContextVar("vx_session_touched", default=None)


# --- 规范化辅助 -------------------------------------------------------------

def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _norm_doi(value):
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:", "", v)
    return v


def _norm_arxiv(value):
    """arXiv 主标识：去掉 vN 版本后缀；v1/v2 属于同一论文的版本标签。"""
    v = str(value or "").strip().lower()
    v = re.sub(r"^arxiv:", "", v)
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?$", v)
    if m:
        return m.group(1)
    return re.sub(r"v\d+$", "", v)


def _arxiv_version(value):
    m = re.search(r"v(\d+)$", str(value or "").strip().lower())
    return ("v" + m.group(1)) if m else ""


def _title_fp(title):
    text = str(title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _author_fp(name):
    return re.sub(r"[\W_]+", "", str(name or "").lower(), flags=re.UNICODE)


def _norm_date(value):
    v = str(value or "").strip()
    if not v:
        return ""
    m = re.match(r"(\d{4})(-\d{2})?(-\d{2})?", v)
    return v[:10] if m and m.group(3) else (v[:7] if m and m.group(2) else (v[:4] if m else v))


def _norm_label(value):
    return str(value or "").strip().lower()


def canonical(record):
    """从记录抽取版本归并所需的规范字段；非学术记录返回 None。"""
    if not isinstance(record, dict):
        return None
    doi = _norm_doi(record.get("doi"))
    raw_arxiv = record.get("arxiv_id") or record.get("arxiv")
    arxiv = _norm_arxiv(raw_arxiv)
    title = str(record.get("title") or "").strip()
    authors = [str(a).strip() for a in (record.get("authors") or []) if str(a).strip()]
    if not (doi or arxiv or title or authors):
        return None
    rtype = str(record.get("record_type") or record.get("type") or "").strip().lower()
    is_retraction = bool(record.get("retraction")) or rtype == "retraction"
    # 显式 version_label 优先；否则采用 arXiv id 自带的 vN 后缀
    version_label = _norm_label(record.get("version_label") or record.get("version")) \
        or _arxiv_version(raw_arxiv)
    if not rtype or rtype == "retraction":
        kind = KIND_RETRACTION if is_retraction else ""
    else:
        kind = KIND_RETRACTION if is_retraction else (
            KIND_PREPRINT if rtype in ("preprint", "arxiv") else KIND_PUBLISHED)
    return {
        "record_key": key_of(record),
        "doi": doi,
        "arxiv_id": arxiv,
        "title": title,
        "title_fp": _title_fp(title),
        "authors": authors,
        "author_fps": sorted({_author_fp(a) for a in authors if _author_fp(a)}),
        "date": _norm_date(record.get("date") or record.get("published_date")),
        "version_label": version_label,
        "kind": kind,
        "is_retraction": is_retraction,
        "retracts_doi": _norm_doi(record.get("retracts_doi")),
        "retracts_arxiv": _norm_arxiv(record.get("retracts_arxiv")),
        "retracts_title": str(record.get("retracts_title") or "").strip(),
        "retraction_reason": str(record.get("retraction_reason") or record.get("reason") or "").strip(),
        "raw": record,
    }


def is_scholarly(record):
    return canonical(record) is not None


# --- 会话：批量提交 / 半批失败回滚 ------------------------------------------

class session:  # noqa: N801 - 上下文管理器按小写风格使用
    """版本归并会话。

    正常退出时统一 finalize 受影响论文并 commit；任何异常都 rollback，
    保证“半批失败”不留部分物化状态，重跑时依靠确定性 event_id 幂等收敛。
    """

    def __init__(self, con):
        self.con = con
        self._tok = None
        self._set_tok = None

    def __enter__(self):
        store.init_version_schema(self.con)
        self._tok = _SESSION_CON.set(self.con)
        self._set_tok = _SESSION_TOUCHED.set(set())
        return self.con

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                for paper_id in sorted(_SESSION_TOUCHED.get() or ()):
                    canonical = _safe_canonical(self.con, paper_id)
                    if store.get_paper(self.con, canonical):
                        _finalize_paper(self.con, canonical)
                self.con.commit()
            else:
                # 半批失败：本批次全部写入（含 finalize 事件）整体回滚，
                # 不留半截物化状态；已提交的历史批次不受影响
                self.con.rollback()
        finally:
            _SESSION_TOUCHED.reset(self._set_tok)
            _SESSION_CON.reset(self._tok)
        return False


def _require_con(con):
    active = _SESSION_CON.get()
    con = con or active
    if con is None:
        raise RuntimeError(
            "version.ingest/process 需要在 version.session(con) 内调用，"
            "或显式传入 sqlite 连接；无会话时不会隐式建库")
    return con


def _safe_canonical(con, paper_id):
    try:
        return store.canonical_paper_id(con, paper_id)
    except Exception:
        return paper_id


def _touch(con, paper_id):
    touched = _SESSION_TOUCHED.get()
    if touched is not None:
        touched.add(paper_id)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _event(con, event_id, paper_id, kind, payload):
    if store.event_exists(con, event_id):
        return False
    seq = store.next_event_seq(con)
    store.append_event(con, event_id, paper_id, seq, kind, payload, _now())
    return True


# --- 身份匹配 ---------------------------------------------------------------

def _paper_by_identifier(con, id_type, id_value):
    hit = store.find_identifier(con, id_type, id_value)
    return hit["paper_id"] if hit else None


def _papers_by_title_fp(con, title_fp):
    if not title_fp:
        return []
    rows = con.execute(
        "SELECT DISTINCT paper_id FROM vx_nodes WHERE json_extract(metadata, '$.title_fp')=?",
        (title_fp,)).fetchall()
    return [r[0] for r in rows]


def _paper_author_fps(con, paper_id):
    fps = set()
    for node in store.nodes_for_paper(con, paper_id):
        if node["kind"] == KIND_RETRACTION:
            continue
        fps.update(node["metadata"].get("author_fps", []))
    return fps


def _paper_doi(con, paper_id):
    for ident in store.identifiers_for_paper(con, paper_id):
        if ident["id_type"] == "doi":
            return ident["id_value"]
    return ""


def _paper_arxiv(con, paper_id):
    for ident in store.identifiers_for_paper(con, paper_id):
        if ident["id_type"] == "arxiv":
            return ident["id_value"]
    return ""


def _resolve_strong_papers(con, rec):
    """返回 (强匹配论文表 {paper_id: [规则...]}, 弱候选 [(paper_id, reason, evidence)])。"""
    strong = {}
    weak = {}

    def add_strong(pid, rule):
        strong.setdefault(pid, []).append(rule)

    def add_weak(pid, rule, evidence):
        weak.setdefault(pid, (rule, evidence))

    if rec["doi"]:
        pid = _paper_by_identifier(con, "doi", rec["doi"])
        if pid:
            add_strong(pid, RULE_DOI)
    if rec["arxiv_id"]:
        pid = _paper_by_identifier(con, "arxiv", rec["arxiv_id"])
        if pid:
            add_strong(pid, RULE_ARXIV)

    # 标题指纹命中只用于收集候选论文；是否自动接链由下面的作者/标识证据
    # （R3 / W1 / W2 / W3）决定，fingerprint 标识本身不构成强证据。
    title_papers = set(_papers_by_title_fp(con, rec["title_fp"])) if rec["title_fp"] else set()
    for pid in sorted(title_papers):
        if pid in strong:
            continue
        existing_doi, existing_arxiv = _paper_doi(con, pid), _paper_arxiv(con, pid)
        ident_conflict = (
            (rec["doi"] and existing_doi and rec["doi"] != existing_doi) or
            (rec["arxiv_id"] and existing_arxiv and rec["arxiv_id"] != existing_arxiv))
        shared = sorted(set(rec["author_fps"]) & _paper_author_fps(con, pid))
        evidence = {"title_fp": rec["title_fp"], "shared_author_fps": shared,
                    "incoming_doi": rec["doi"], "existing_doi": existing_doi,
                    "incoming_arxiv": rec["arxiv_id"], "existing_arxiv": existing_arxiv}
        if ident_conflict:
            # 标识明确不同：有共有作者说明确属身份存疑（W3）；无作者佐证仅标题相同（W2）
            if shared:
                add_weak(pid, CANDIDATE_IDENT_CONFLICT, evidence)
            else:
                add_weak(pid, CANDIDATE_TITLE_ONLY, evidence)
        elif rec["author_fps"] and shared and set(rec["author_fps"]) == _paper_author_fps(con, pid):
            add_strong(pid, RULE_TITLE_AUTHORS)
        elif shared:
            add_weak(pid, CANDIDATE_TITLE_AUTHOR, evidence)
        else:
            add_weak(pid, CANDIDATE_TITLE_ONLY, evidence)
    return strong, weak


def _anchor_rank(con, paper_id):
    # 按 paper_id 锚点前缀判断强度（paper:doi: < paper:arxiv: < paper:fp:）
    prefix = _anchor_prefix(paper_id)
    return {"doi": 0, "arxiv": 1, "fp": 2}.get(prefix, 2)


def _pick_survivor(con, paper_ids):
    return sorted(paper_ids, key=lambda p: (_anchor_rank(con, p), p))[0]


# --- 论文 / 节点写入 --------------------------------------------------------

def _new_paper_id(rec):
    """新论文的稳定 id：锚点前缀 + 记录键哈希，保证不同论文不会因同标题/同标识
    碰撞而共用 id；锚点值本身保存在 vx_identifiers 中负责匹配与解析。"""
    suffix = _sha(rec["record_key"])[:16]
    if rec["doi"]:
        return "paper:doi:" + suffix
    if rec["arxiv_id"]:
        return "paper:arxiv:" + suffix
    return "paper:fp:" + suffix


def _anchor_prefix(paper_id):
    return paper_id.split(":", 2)[1] if paper_id.startswith("paper:") else "fp"


def _node_id(rec):
    return "n_" + _sha(rec["record_key"])[:16]


def _ensure_paper(con, rec):
    paper_id = _new_paper_id(rec)
    if store.get_paper(con, paper_id) is None:
        anchor_type, anchor_value = _new_paper_id_anchor(rec)
        _event(con, f"paper_created:{paper_id}", paper_id, "paper_created",
               {"paper_id": paper_id, "anchor_type": anchor_type, "anchor_value": anchor_value})
        store.put_paper(con, paper_id, "open", None, None, _now(), _now())
    _record_identifiers(con, paper_id, rec)
    return paper_id


def _rename_paper(con, old_id, new_id, reason, rules):
    """同一篇论文获得更强锚点（如 arXiv 链迎来 DOI）：就地改名并留别名。"""
    if old_id == new_id or store.get_paper(con, old_id) is None:
        return new_id
    ev = f"paper_renamed:{old_id}:{new_id}"
    if store.get_paper(con, new_id) is None:
        old = store.get_paper(con, old_id)
        if _event(con, ev, new_id, "paper_renamed",
                  {"old_paper": old_id, "new_paper": new_id, "reason": reason, "rules": rules}):
            store.put_paper(con, new_id, old["status"], old["formal_node_id"],
                            old["formal_basis"], old["created_ts"], _now())
            store.reassign_nodes(con, old_id, new_id)
            store.reassign_identifiers(con, old_id, new_id)
            store.reassign_retractions(con, old_id, new_id)
            store.reassign_records(con, old_id, new_id)
            store.add_alias(con, old_id, new_id, ev)
            con.execute("DELETE FROM vx_papers WHERE paper_id=?", (old_id,))
    else:
        # 目标 id 已存在：退化为普通并链
        _merge_papers(con, new_id, [old_id], rules)
    _touch(con, new_id)
    return new_id


def _new_paper_id_anchor(rec):  # 显式锚点描述，供事件 payload 使用
    if rec["doi"]:
        return ("doi", rec["doi"])
    if rec["arxiv_id"]:
        return ("arxiv", rec["arxiv_id"])
    return ("fingerprint", rec["title_fp"] or _sha(rec["record_key"])[:16])

def _record_identifiers(con, paper_id, rec, event_prefix=None):
    for id_type, id_value in (("doi", rec["doi"]), ("arxiv", rec["arxiv_id"]),
                              ("fingerprint", rec["title_fp"])):
        if not id_value:
            continue
        hit = store.find_identifier(con, id_type, id_value)
        if hit is None:
            ev = f"{event_prefix or 'ident'}:{id_type}:{id_value}:{paper_id}"
            _event(con, ev, paper_id, "identifier_seen",
                   {"id_type": id_type, "id_value": id_value, "paper_id": paper_id})
            store.put_identifier(con, id_type, id_value, paper_id, ev)


def _merge_papers(con, survivor, others, rules):
    """把 others 整体并入 survivor（节点/标识/撤回/记录索引随之迁移）。"""
    for old in sorted(others):
        if old == survivor or store.get_paper(con, old) is None:
            continue
        ev = f"paper_merged:{old}:{survivor}"
        if _event(con, ev, survivor, "paper_merged",
                  {"old_paper": old, "new_paper": survivor, "rules": sorted(rules)}):
            store.reassign_nodes(con, old, survivor)
            store.reassign_identifiers(con, old, survivor)
            store.reassign_retractions(con, old, survivor)
            store.reassign_records(con, old, survivor)
            store.add_alias(con, old, survivor, ev)
            store.mark_paper_status(con, old, "merged")
            store.delete_candidate_pair(con, survivor, old)
            paper = store.get_paper(con, survivor)
            store.put_paper(con, survivor, paper["status"] or "open",
                            paper["formal_node_id"], paper["formal_basis"],
                            paper["created_ts"], _now())
        _touch(con, survivor)
        _touch(con, old)


def _metadata_conflicts(existing_meta, rec):
    diffs = []
    for field in ("title", "date", "version_label", "doi", "arxiv_id"):
        new_val = rec.get(field, "")
        old_val = existing_meta.get(field, "")
        if new_val and old_val and str(new_val) != str(old_val):
            diffs.append({"field": field, "existing": old_val, "incoming": new_val})
    old_authors, new_authors = existing_meta.get("authors", []), rec["authors"]
    if old_authors and new_authors and old_authors != new_authors:
        diffs.append({"field": "authors", "existing": old_authors, "incoming": new_authors})
    return diffs


def _find_duplicate_node(con, paper_id, rec):
    """重复版本：同 kind/版本标签/日期，且至少共享一个强标识或标题指纹一致。

    仅 kind/label/date 相同不足以判重——桥接记录（携带第二个标识把两条链
    连起来）或元数据冲突快照（同标识但日期/标题不同）都必须保留为独立节点；
    这里要求所有版本判别字段（日期、标签、标题指纹、DOI/arXiv 标识）一致，
    仅来源 id 不同的镜像记录才判为 duplicate。
    """
    for node in store.nodes_for_paper(con, paper_id):
        meta = node["metadata"]
        same_shape = (
            node["kind"] == rec["kind"]
            and node["version_label"] == rec["version_label"]
            and (node["node_date"] or "") == (rec["date"] or "")
            and meta.get("title_fp") == rec["title_fp"])
        if not same_shape:
            continue
        shares_anchor = (
            (rec["doi"] and meta.get("doi") == rec["doi"]) or
            (rec["arxiv_id"] and meta.get("arxiv_id") == rec["arxiv_id"]) or
            (not rec["doi"] and not rec["arxiv_id"] and meta.get("title_fp") == rec["title_fp"]))
        if shares_anchor:
            return node
    return None


# --- 撤回 -------------------------------------------------------------------

def _resolve_retraction_target(con, rec):
    for id_type, value in (("doi", rec["retracts_doi"]), ("arxiv", rec["retracts_arxiv"])):
        if value:
            pid = _paper_by_identifier(con, id_type, value)
            if pid:
                return pid, id_type
    if rec["retracts_title"]:
        pids = _papers_by_title_fp(con, _title_fp(rec["retracts_title"]))
        if len(pids) == 1:
            return pids[0], "title_fp"
    # 撤回通知自身携带的标识也可能直接指向被撤论文
    if rec["doi"]:
        pid = _paper_by_identifier(con, "doi", rec["doi"])
        if pid:
            return pid, "doi"
    if rec["arxiv_id"]:
        pid = _paper_by_identifier(con, "arxiv", rec["arxiv_id"])
        if pid:
            return pid, "arxiv"
    return None, None


# --- 主入口：摄取一条记录 ---------------------------------------------------

def ingest(con, record):
    """把一条学术记录并入版本子系统，返回 {paper_id, node_id, role, candidates}。"""
    con = _require_con(con)
    rec = canonical(record)
    if rec is None:
        return {"paper_id": None, "node_id": None, "role": "non_scholarly", "candidates": []}

    # 重放保护：同源记录已索引则直接回显，重复运行不产生新事件/新节点
    already = store.lookup_record(con, rec["record_key"])
    if already is not None:
        return {"paper_id": already["paper_id"], "node_id": already["node_id"],
                "role": already["role"], "candidates": [], "idempotent": True}

    if rec["is_retraction"]:
        return _ingest_retraction(con, rec)

    strong, weak = _resolve_strong_papers(con, rec)

    merged_this_ingest = set()
    if strong:
        survivor = _pick_survivor(con, strong.keys())
        rules = sorted({r for rs in strong.values() for r in rs})
        # 强匹配可能命中多条原本独立的链：本记录同时携带双方标识，即桥接证据，
        # 全部并入 survivor（paper_merged 事件保存每条链与所依据的规则）
        if len(strong) > 1:
            merged_this_ingest = set(strong) - {survivor}
            _merge_papers(con, survivor, merged_this_ingest, rules)
        paper_id = survivor
        # 本记录自带更强锚点（典型：arXiv 链先建，期刊 DOI 后到）-> 改名并留别名
        target_id = _new_paper_id(rec)
        if _anchor_rank(con, target_id) < _anchor_rank(con, paper_id):
            paper_id = _rename_paper(con, paper_id, target_id, "stronger_anchor_arrived", rules)
    else:
        paper_id = _ensure_paper(con, rec)

    # 弱证据：保留候选、暂不归并（强标识桥接的链已在上面的 merge 中处理）
    candidates = []
    for other_pid, (reason, evidence) in sorted(weak.items()):
        other_pid = store.canonical_paper_id(con, other_pid)
        if other_pid == paper_id:
            continue
        cid = "cand:" + _sha(f"{paper_id}|{other_pid}|{reason}|{rec['record_key']}")[:16]
        ev = f"candidate_retained:{cid}"
        if _event(con, ev, paper_id, "candidate_retained",
                  {"paper_id": paper_id, "other_paper": other_pid, "reason": reason,
                   "rule_description": CANDIDATE_RULES[reason], "evidence": evidence,
                   "record_key": rec["record_key"]}):
            store.add_candidate(con, cid, paper_id, other_pid, reason, evidence, _now())
        candidates.append({"other_paper": other_pid, "reason": reason})

    _record_identifiers(con, paper_id, rec, event_prefix=f"ident:{rec['record_key']}")

    dup = None if merged_this_ingest else _find_duplicate_node(con, paper_id, rec)
    if dup is not None:
        ev = f"duplicate_record:{rec['record_key']}:{dup['node_id']}"
        _event(con, ev, paper_id, "duplicate_record",
               {"record_key": rec["record_key"], "duplicate_of_node": dup["node_id"],
                "kind": rec["kind"], "version_label": rec["version_label"], "node_date": rec["date"]})
        store.index_record(con, rec["record_key"], paper_id, dup["node_id"], "duplicate")
        _touch(con, paper_id)
        return {"paper_id": paper_id, "node_id": dup["node_id"], "role": "duplicate",
                "candidates": candidates}

    node_id = _node_id(rec)
    node_meta = {k: rec[k] for k in ("doi", "arxiv_id", "title", "title_fp", "authors",
                                     "author_fps", "date", "version_label", "kind", "record_key")}
    # 元数据冲突：与链上任意同标识版本比对，差异全部留痕，旧元数据绝不覆盖
    conflicts = []
    for node in store.nodes_for_paper(con, paper_id):
        if node["kind"] == KIND_RETRACTION:
            continue
        conflicts.extend(_metadata_conflicts(node["metadata"], rec))
    if conflicts:
        _event(con, f"metadata_conflict:{rec['record_key']}", paper_id, "metadata_conflict",
               {"record_key": rec["record_key"], "node_id": node_id, "conflicts": conflicts})

    _event(con, f"node_seen:{rec['record_key']}", paper_id, "node_seen",
           {"node_id": node_id, "paper_id": paper_id, "kind": rec["kind"],
            "record_key": rec["record_key"], "version_label": rec["version_label"],
            "node_date": rec["date"], "metadata": node_meta})
    store.put_node(con, {"node_id": node_id, "paper_id": paper_id, "kind": rec["kind"],
                         "ord": None, "record_key": rec["record_key"],
                         "version_label": rec["version_label"], "node_date": rec["date"],
                         "metadata": node_meta, "ingest_ts": _now()})
    store.index_record(con, rec["record_key"], paper_id, node_id, "canonical")
    _touch(con, paper_id)
    return {"paper_id": paper_id, "node_id": node_id, "role": "canonical",
            "candidates": candidates}


def _ingest_retraction(con, rec):
    target_pid, via = _resolve_retraction_target(con, rec)
    dangling = target_pid is None
    if dangling:
        target_pid = _ensure_paper(con, rec)
    _record_identifiers(con, target_pid, rec, event_prefix=f"ident:{rec['record_key']}")

    node_id = _node_id(rec)
    node_meta = {k: rec[k] for k in ("doi", "arxiv_id", "title", "title_fp", "authors",
                                     "date", "version_label", "record_key",
                                     "retracts_doi", "retracts_arxiv", "retracts_title",
                                     "retraction_reason")}
    _event(con, f"node_seen:{rec['record_key']}", target_pid, "node_seen",
           {"node_id": node_id, "paper_id": target_pid, "kind": KIND_RETRACTION,
            "record_key": rec["record_key"], "version_label": rec["version_label"],
            "node_date": rec["date"], "metadata": node_meta})
    store.put_node(con, {"node_id": node_id, "paper_id": target_pid, "kind": KIND_RETRACTION,
                         "ord": None, "record_key": rec["record_key"],
                         "version_label": rec["version_label"], "node_date": rec["date"],
                         "metadata": node_meta, "ingest_ts": _now()})

    target_node = None
    if not dangling:
        published = [n for n in store.nodes_for_paper(con, target_pid)
                     if n["kind"] == KIND_PUBLISHED]
        if published:
            target_node = sorted(published, key=_desc_then_id)[0]["node_id"]
    retraction_id = "ret:" + _sha(rec["record_key"])[:16]
    payload = {"retraction_id": retraction_id, "paper_id": target_pid, "node_id": node_id,
               "target_node_id": target_node, "record_key": rec["record_key"],
               "reason": rec["retraction_reason"], "retracted_date": rec["date"],
               "resolved_via": via, "dangling": dangling, "metadata": node_meta}
    _event(con, f"retraction_{'dangling' if dangling else 'recorded'}:{rec['record_key']}",
           target_pid, "retraction_dangling" if dangling else "retraction_recorded", payload)
    store.add_retraction(con, {"retraction_id": retraction_id, "paper_id": target_pid,
                               "node_id": node_id, "target_node_id": target_node,
                               "record_key": rec["record_key"], "reason": rec["retraction_reason"],
                               "retracted_date": rec["date"], "metadata": node_meta,
                               "ingest_ts": _now()})
    store.index_record(con, rec["record_key"], target_pid, node_id, "retraction")
    _touch(con, target_pid)
    return {"paper_id": target_pid, "node_id": node_id, "role": "retraction",
            "candidates": [], "dangling": dangling}


# --- 链排序 / 边 / 正式版选择（纯规则，可对任意连接重算） -------------------

def _node_sort(node):
    # 日期优先，其次版本标签；同票以 node_id 确定性打破（与到达顺序无关）
    return (node["node_date"] or "", node["version_label"] or "", node["node_id"])


def _inv(s):
    """字符串降序而 node_id 仍升序：把日期/标签字符按码点取反参与排序。"""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in (s or ""))


def _desc_then_id(node):
    return (_inv(node["node_date"]), _inv(node["version_label"]), node["node_id"])


def _order_chain(nodes):
    preprints = sorted((n for n in nodes if n["kind"] == KIND_PREPRINT),
                       key=lambda n: _node_sort(n))
    published = sorted((n for n in nodes if n["kind"] == KIND_PUBLISHED),
                       key=lambda n: _node_sort(n))
    retractions = sorted((n for n in nodes if n["kind"] == KIND_RETRACTION),
                         key=lambda n: _node_sort(n))
    return preprints + published + retractions


def _select_formal(con, paper_id, nodes):
    published = [n for n in nodes if n["kind"] == KIND_PUBLISHED]
    retractions = store.retractions_for_paper(con, paper_id)
    retracted_node_ids = {r["target_node_id"] for r in retractions if r["target_node_id"]}
    retraction_node_ids = {r["node_id"] for r in retractions}
    if not published:
        return None, {
            "rule": "no_published_version",
            "selection_principle": "evidence_based_not_arrival_order",
            "selected": None,
            "reason": "链上尚无 kind=published 节点，正式版空缺；当前版本取最新预印本",
            "candidates": [],
            "retraction_ids": [r["retraction_id"] for r in retractions],
            "decided_ts": _now(),
        }
    # 日期降序 -> 版本标签降序 -> node_id 升序（确定性，不依赖到达顺序）
    ranked = sorted(published, key=_desc_then_id)
    chosen = ranked[0]
    basis = {
        "rule": "latest_published_by_date_then_label_then_node_id",
        "selection_principle": "evidence_based_not_arrival_order",
        "selected": chosen["node_id"],
        "candidates": [{
            "node_id": n["node_id"],
            "node_date": n["node_date"],
            "version_label": n["version_label"],
            "retracted": n["node_id"] in retracted_node_ids,
        } for n in ranked],
        "retracted": chosen["node_id"] in retracted_node_ids
                    or chosen["node_id"] in retraction_node_ids,
        "retraction_ids": [r["retraction_id"] for r in retractions],
        "conflict_events": [e["event_id"] for e in history(con, paper_id)
                            if e["kind"] == "metadata_conflict"],
        "decided_ts": _now(),
    }
    return chosen["node_id"], basis


def _finalize_paper(con, paper_id):
    paper = store.get_paper(con, paper_id)
    if paper is None or paper["status"] == "merged":
        return
    nodes = store.nodes_for_paper(con, paper_id)
    ordered = _order_chain(nodes)
    for ord_, node in enumerate(ordered):
        node["ord"] = ord_
        node["paper_id"] = paper_id
        store.put_node(con, node)

    edges = []
    chain = [n for n in ordered if n["kind"] != KIND_RETRACTION]
    for earlier, later in zip(chain, chain[1:]):
        edges.append((earlier["node_id"], later["node_id"], "supersedes"))
    # 悬空撤回在正式稿到达后补齐指向：以最新 published 节点为撤回目标
    rets = store.retractions_for_paper(con, paper_id)
    latest_published = None
    published = [n for n in ordered if n["kind"] == KIND_PUBLISHED]
    if published:
        latest_published = sorted(published, key=_desc_then_id)[0]["node_id"]
    for ret in rets:
        target = ret["target_node_id"] or latest_published
        if target and ret["target_node_id"] != target:
            con.execute("UPDATE vx_retractions SET target_node_id=? WHERE retraction_id=?",
                        (target, ret["retraction_id"]))
        if ret["node_id"] and target:
            edges.append((ret["node_id"], target, "retracts"))
    store.replace_edges(con, paper_id, edges)

    formal_node_id, basis = _select_formal(con, paper_id, nodes)
    basis_hash = _sha(json.dumps(basis, sort_keys=True, ensure_ascii=False))[:12]
    _event(con, f"formal_selected:{paper_id}:{formal_node_id or 'none'}:{basis_hash}",
           paper_id, "formal_selected",
           {"paper_id": paper_id, "selected": formal_node_id, "basis": basis})
    status = "retracted" if store.retractions_for_paper(con, paper_id) else "open"
    store.put_paper(con, paper_id, status, formal_node_id, basis,
                    paper["created_ts"], _now())


# --- 历史重算 ---------------------------------------------------------------

def rebuild(con):
    """清空物化状态（保留原始事件流），按 vx_events 全局顺序重放并重算。

    事件流是不可变事实源：``formal_selected`` 等历史决策事件保留在原表中
    作为审计轨迹，重算产生的新选择以 ``formal_recomputed`` 写入，规则变化
    后可与历史依据逐字段比对。
    """
    con = _require_con(con)
    events = store.all_events(con)
    for tbl in ("vx_record_index", "vx_retractions", "vx_candidates", "vx_edges",
                "vx_nodes", "vx_identifiers", "vx_paper_aliases", "vx_papers"):
        con.execute(f"DELETE FROM {tbl}")
    for ev in events:
        _replay_event(con, ev)
    for paper in store.all_papers(con):
        if paper["status"] != "merged":
            _recompute_paper(con, paper["paper_id"])
    con.commit()
    return {"replayed_events": len(events), "papers": len(store.all_papers(con))}


def _recompute_paper(con, paper_id):
    """与 _finalize_paper 相同的重算逻辑，但重算选择事件用独立 kind 落账。"""
    paper = store.get_paper(con, paper_id)
    if paper is None or paper["status"] == "merged":
        return
    nodes = store.nodes_for_paper(con, paper_id)
    ordered = _order_chain(nodes)
    for ord_, node in enumerate(ordered):
        node["ord"] = ord_
        node["paper_id"] = paper_id
        store.put_node(con, node)

    edges = []
    chain = [n for n in ordered if n["kind"] != KIND_RETRACTION]
    for earlier, later in zip(chain, chain[1:]):
        edges.append((earlier["node_id"], later["node_id"], "supersedes"))
    rets = store.retractions_for_paper(con, paper_id)
    latest_published = None
    published = [n for n in ordered if n["kind"] == KIND_PUBLISHED]
    if published:
        latest_published = sorted(published, key=_desc_then_id)[0]["node_id"]
    for ret in rets:
        target = ret["target_node_id"] or latest_published
        if target and ret["target_node_id"] != target:
            con.execute("UPDATE vx_retractions SET target_node_id=? WHERE retraction_id=?",
                        (target, ret["retraction_id"]))
        if ret["node_id"] and target:
            edges.append((ret["node_id"], target, "retracts"))
    store.replace_edges(con, paper_id, edges)

    formal_node_id, basis = _select_formal(con, paper_id, nodes)
    basis_hash = _sha(json.dumps(basis, sort_keys=True, ensure_ascii=False))[:12]
    _event(con, f"formal_recomputed:{paper_id}:{formal_node_id or 'none'}:{basis_hash}",
           paper_id, "formal_recomputed",
           {"paper_id": paper_id, "selected": formal_node_id, "basis": basis})
    status = "retracted" if store.retractions_for_paper(con, paper_id) else "open"
    store.put_paper(con, paper_id, status, formal_node_id, basis,
                    paper["created_ts"], _now())


def _replay_event(con, ev):
    kind, payload, paper_id, ts = ev["kind"], ev["payload"], ev["paper_id"], ev["ingest_ts"]
    if kind == "paper_created":
        if store.get_paper(con, paper_id) is None:
            store.put_paper(con, paper_id, "open", None, None, ts, ts)
    elif kind == "identifier_seen":
        store.put_identifier(con, payload["id_type"], payload["id_value"],
                             payload["paper_id"], ev["event_id"])
    elif kind == "node_seen":
        p = payload
        if store.get_node(con, p["node_id"]) is None:
            store.put_node(con, {"node_id": p["node_id"], "paper_id": p["paper_id"],
                                 "kind": p["kind"], "ord": None, "record_key": p["record_key"],
                                 "version_label": p.get("version_label"),
                                 "node_date": p.get("node_date"),
                                 "metadata": p["metadata"], "ingest_ts": ts})
            role = "retraction" if p["kind"] == KIND_RETRACTION else "canonical"
            store.index_record(con, p["record_key"], p["paper_id"], p["node_id"], role)
    elif kind == "duplicate_record":
        store.index_record(con, payload["record_key"], paper_id,
                           payload["duplicate_of_node"], "duplicate")
    elif kind == "paper_merged":
        old, new = payload["old_paper"], payload["new_paper"]
        store.reassign_nodes(con, old, new)
        store.reassign_identifiers(con, old, new)
        store.reassign_retractions(con, old, new)
        store.reassign_records(con, old, new)
        store.add_alias(con, old, new, ev["event_id"])
        if store.get_paper(con, old):
            store.mark_paper_status(con, old, "merged")
    elif kind == "paper_renamed":
        old, new = payload["old_paper"], payload["new_paper"]
        old_paper = store.get_paper(con, old)
        if old_paper is not None and store.get_paper(con, new) is None:
            store.put_paper(con, new, old_paper["status"], old_paper["formal_node_id"],
                            old_paper["formal_basis"], old_paper["created_ts"], ts)
            store.reassign_nodes(con, old, new)
            store.reassign_identifiers(con, old, new)
            store.reassign_retractions(con, old, new)
            store.reassign_records(con, old, new)
            store.add_alias(con, old, new, ev["event_id"])
            con.execute("DELETE FROM vx_papers WHERE paper_id=?", (old,))
    elif kind == "candidate_retained":
        cid = "cand:" + _sha(
            f"{payload['paper_id']}|{payload['other_paper']}|{payload['reason']}|"
            f"{payload['record_key']}")[:16]
        store.add_candidate(con, cid, payload["paper_id"], payload["other_paper"],
                            payload["reason"], payload["evidence"], ts)
    elif kind in ("retraction_recorded", "retraction_dangling"):
        store.add_retraction(con, {
            "retraction_id": payload["retraction_id"], "paper_id": payload["paper_id"],
            "node_id": payload["node_id"], "target_node_id": payload.get("target_node_id"),
            "record_key": payload["record_key"], "reason": payload.get("reason"),
            "retracted_date": payload.get("retracted_date"),
            "metadata": payload["metadata"], "ingest_ts": ts})
    elif kind in ("formal_selected", "formal_recomputed"):
        # 历史选择仅作审计保留；重算结束后由 _recompute_paper 按现行规则重新选择
        return
    elif kind == "metadata_conflict":
        return  # 纯审计事件，物化状态只依赖节点元数据本身


# --- 查询接口（doi_map / dedup / 测试共用） ---------------------------------

def paper_for_record(con, record_key):
    con = _require_con(con)
    hit = store.lookup_record(con, record_key)
    return store.canonical_paper_id(con, hit["paper_id"]) if hit else None


def current_formal(con, paper_id):
    """返回论文当前正式版节点（含该版完整元数据）；无正式版返回 None。"""
    con = _require_con(con)
    canonical = store.canonical_paper_id(con, paper_id)
    paper = store.get_paper(con, canonical)
    if paper is None:
        return None
    if paper["formal_node_id"]:
        return store.get_node(con, paper["formal_node_id"])
    # 会话尚未 finalize（批次进行中）：按同一规则现场计算，保证查询即时可用
    node_id, _ = _select_formal(con, canonical, store.nodes_for_paper(con, canonical))
    return store.get_node(con, node_id) if node_id else None


def current_formal_basis(con, paper_id):
    con = _require_con(con)
    canonical = store.canonical_paper_id(con, paper_id)
    paper = store.get_paper(con, canonical)
    if paper is None:
        return None
    if paper["formal_basis"] is not None:
        return paper["formal_basis"]
    _, basis = _select_formal(con, canonical, store.nodes_for_paper(con, canonical))
    return basis


def current_version(con, paper_id):
    """当前可引用版本：优先正式版；没有正式版时回退最新预印本。"""
    con = _require_con(con)
    paper_id = store.canonical_paper_id(con, paper_id)
    formal = current_formal(con, paper_id)
    if formal is not None:
        return formal
    preprints = [n for n in store.nodes_for_paper(con, paper_id)
                 if n["kind"] == KIND_PREPRINT]
    return sorted(preprints, key=_desc_then_id)[0] if preprints else None


def version_chain(con, paper_id):
    con = _require_con(con)
    paper_id = store.canonical_paper_id(con, paper_id)
    return {"paper": store.get_paper(con, paper_id),
            "nodes": store.nodes_for_paper(con, paper_id),
            "edges": store.edges_for_paper(con, paper_id),
            "retractions": store.retractions_for_paper(con, paper_id),
            "candidates": store.candidates_for_paper(con, paper_id),
            "aliases": store.aliases_for_paper(con, paper_id)}


def retractions(con, paper_id):
    con = _require_con(con)
    return store.retractions_for_paper(con, store.canonical_paper_id(con, paper_id))


def candidates(con, paper_id=None):
    con = _require_con(con)
    if paper_id is None:
        return store.all_candidates(con)
    return store.candidates_for_paper(con, store.canonical_paper_id(con, paper_id))


def history(con, paper_id):
    """返回该论文的只追加决策历史（含正式版选择依据与冲突/归并事件）。

    改名/并链后旧 paper_id 的事件仍以原始归属保留，这里按别名链汇总，
    保证任一历史 id 都能找回完整决策轨迹。
    """
    con = _require_con(con)
    canonical = store.canonical_paper_id(con, paper_id)
    ids = {canonical, paper_id}
    for alias in store.aliases_for_paper(con, canonical):
        ids.add(alias["alias_paper_id"])
    out = []
    for ev in store.all_events(con):
        if ev["paper_id"] in ids or ev["payload"].get("new_paper") == canonical \
                or ev["payload"].get("old_paper") in ids:
            out.append(ev)
    return out


def merge_history(con, paper_id):
    con = _require_con(con)
    canonical = store.canonical_paper_id(con, paper_id)
    out = []
    for ev in store.all_events(con):
        if ev["kind"] in ("paper_merged", "paper_renamed") and (
                ev["paper_id"] == canonical or
                ev["payload"].get("new_paper") == canonical or
                ev["payload"].get("old_paper") == paper_id):
            out.append(ev)
    return out


def is_duplicate_record(con, record_key):
    con = _require_con(con)
    hit = store.lookup_record(con, record_key)
    return bool(hit and hit["role"] == "duplicate")


# --- 流水线阶段函数 ---------------------------------------------------------

def process(record):
    """流水线阶段：在 version.session 内把记录并入版本子系统并回填映射字段。"""
    rec = dict(record)
    if not is_scholarly(rec):
        rec.setdefault("_stage_version", "skipped_non_scholarly")
        return rec
    con = _SESSION_CON.get()
    if con is None:
        raise RuntimeError("version.process 必须在 version.session(con) 内由 pipeline 调用")
    result = ingest(con, rec)
    paper_id = store.canonical_paper_id(con, result["paper_id"]) if result["paper_id"] else None
    rec["_stage_version"] = "merged" if result["role"] != "non_scholarly" else "skipped"
    rec["_version_paper_id"] = paper_id
    rec["_version_node_id"] = result["node_id"]
    rec["_version_role"] = result["role"]
    formal = current_formal(con, result["paper_id"]) if result["paper_id"] else None
    if formal is not None:
        rec["_version_formal_node_id"] = formal["node_id"]
        rec["_version_formal_doi"] = formal["metadata"].get("doi", "")
    return rec
