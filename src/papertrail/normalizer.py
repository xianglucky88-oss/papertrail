"""单一职责：字段规整/规范化（对记录做单一职责变换：normalizer）。

通用字段（src/name/value）保持既有契约；学术记录（doi/arxiv_id/title/
authors/date/version_label/record_type 等）做去空白、小写、列表化与
日期精度规整，输出的仍是原始 dict 的副本，不做任何归并判断。
"""
import re


def _norm_doi(value):
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v)
    return re.sub(r"^doi:", "", v)


def _norm_arxiv(value):
    # 主标识去掉 vN 后缀；版本差异由 version_label 表达
    v = str(value or "").strip().lower()
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?$", v)
    if m:
        return m.group(1)
    return re.sub(r"^arxiv:", "", v)


def _norm_date(value):
    v = str(value or "").strip()
    if not v:
        return ""
    return v[:10] if len(v) >= 10 else v


def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))

    if "doi" in rec:
        rec["doi"] = _norm_doi(rec.get("doi"))
    if "arxiv_id" in rec:
        raw_arxiv = rec.get("arxiv_id")
        rec["arxiv_id"] = _norm_arxiv(raw_arxiv)
        m = re.search(r"v(\d+)$", str(raw_arxiv or "").strip().lower())
        if m and not rec.get("version_label"):
            rec["version_label"] = "v" + m.group(1)
    if "title" in rec:
        rec["title"] = str(rec.get("title", "")).strip()
    if "authors" in rec:
        authors = rec.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r";|,| and ", authors) if a.strip()]
        rec["authors"] = [str(a).strip() for a in authors if str(a).strip()]
    if "date" in rec:
        rec["date"] = _norm_date(rec.get("date"))
    if "version_label" in rec:
        rec["version_label"] = str(rec.get("version_label", "")).strip().lower()
    if "record_type" in rec:
        rec["record_type"] = str(rec.get("record_type", "")).strip().lower()
    if "retraction_reason" in rec:
        rec["retraction_reason"] = str(rec.get("retraction_reason", "")).strip()
    return rec
