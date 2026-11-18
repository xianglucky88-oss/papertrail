"""单一职责：字段规整/规范化。

兼容两类输入：
1. 旧版通用记录（id/src/name/value/...）——仅做原有规整；
2. 论文记录（doi/title/authors/...）——把原始署名行规范成有序 bylines，
   作者顺序（order）与后续阶段解析出的作者身份（author_id）分开存放。
"""

def _norm_text(v):
    return " ".join(str(v or "").split()).strip()

def normalize_orcid(v):
    """提取 ORCID 本体（xxxx-xxxx-xxxx-xxxx）；格式不合法返回空串。"""
    s = _norm_text(v).upper()
    if not s:
        return ""
    s = s.removeprefix("HTTPS://").removeprefix("HTTP://").removeprefix("ORCID.ORG/")
    s = s.strip("/ ")
    parts = s.split("-")
    if len(parts) != 4 or not all(p.isdigit() and len(p) == 4 for p in parts):
        return ""
    digits = "".join(parts)
    # ORCID 使用 ISO 7064 MOD 11-2 校验位
    total = 0
    for ch in digits[:15]:
        total = (total + int(ch)) * 2
    check = (12 - (total % 11)) % 11
    check_char = "X" if check == 10 else str(check)
    if check_char != digits[15]:
        return ""
    return "-".join(parts)

def normalize_byline(raw, order):
    name = _norm_text(raw.get("name"))
    orcid = normalize_orcid(raw.get("orcid", ""))
    affiliations = [a for a in (_norm_text(x) for x in raw.get("affiliations", [])) if a]
    byline = {
        "order": order,                      # 论文内署名顺序，独立于作者身份
        "name": name,
        "orcid": orcid,
        "affiliations": affiliations,
        "anonymous": bool(raw.get("anonymous", False)) or not name,
        "raw_orcid": _norm_text(raw.get("orcid", "")),
    }
    if raw.get("previous_names"):
        byline["previous_names"] = [_norm_text(x) for x in raw["previous_names"] if _norm_text(x)]
    return byline

def process(record):
    rec = dict(record)
    if "authors" in rec or "doi" in rec or "title" in rec:
        rec["doi"] = _norm_text(rec.get("doi")).lower()
        rec["title"] = _norm_text(rec.get("title"))
        rec["venue"] = _norm_text(rec.get("venue"))
        rec["version_of"] = _norm_text(rec.get("version_of")).lower()
        rec["bylines"] = [normalize_byline(a, i) for i, a in enumerate(rec.get("authors", []))]
        rec.pop("authors", None)
        rec.setdefault("_stage_normalizer", "paper")
        return rec
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))
    return rec
