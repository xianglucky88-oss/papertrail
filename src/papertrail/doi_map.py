"""单一职责：派生 DOI 规范键（doi_map）。

不同来源会给出形如 ``10.1000/ABC``、``https://doi.org/10.1000/abc``、
``doi:10.1000/abc`` 的同一 DOI。这里统一成规范形式，供 :mod:`keyer`
作为稳定主键、供 :mod:`dedup` 判断重复。上游字段漂移（``doi`` / ``DOI``
/ ``Doi``）也在这里吸收。

本模块是纯函数、确定性变换：同一输入永远得到同一规范键。
"""
from __future__ import annotations

import re
from urllib.parse import unquote

DOI_KEYS = ("doi", "DOI", "Doi", "canonical_doi")

# 常见 DOI URL / 前缀形式
_PREFIX_RE = re.compile(
    r"^\s*(?:https?://(?:dx\.)?doi\.org/|doi:\s*|doi\s+|info:doi/)",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
_DOI_BODY_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def canonical_doi(value):
    """把任意 DOI 表示规范成 ``10.xxxx/suffix``（小写）；不合法返回 ``None``。"""
    if value is None:
        return None
    text = unquote(str(value)).strip()
    if not text:
        return None
    text = _PREFIX_RE.sub("", text)
    text = text.strip().rstrip(".,;")
    text = _WS_RE.sub("", text)
    if not text:
        return None
    if not _DOI_BODY_RE.match(text):
        return None
    return text.lower()


def _first(record, keys):
    for key in keys:
        value = record.get(key)
        if value:
            return value
    return None


def canonical_key(record):
    """规范主键：有 DOI 用 ``doi:<规范 DOI>``，否则返回 ``None`` 交回 keyer。"""
    doi = canonical_doi(_first(record, DOI_KEYS))
    return f"doi:{doi}" if doi else None


def process(record):
    rec = dict(record)
    doi = canonical_doi(_first(rec, DOI_KEYS))
    if doi:
        rec["_doi"] = doi
        rec["_canonical_key"] = f"doi:{doi}"
    rec["_stage_doi_map"] = "ok"
    return rec
