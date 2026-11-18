"""单一职责：判定版本状态（预印本 vs 正式版）。

版本状态必须与记录写入在 **同一个提交** 里生效，否则重启后可能出现
"记录是正式版、版本状态还是预印本"的裂缝。因此本模块只做纯判定，把
结果写进记录的 ``_version`` / ``_version_rank``；由 :mod:`store` 在同一
事务里落 ``record_versions`` 并在 ``merge`` 中拒绝降级。

秩：正式版 > 预印本 > 未知。同一 DOI 上正式版永远覆盖预印本，反之不行。
"""
from __future__ import annotations

VERSION_RANK = {
    "published": 3,
    "accepted": 3,
    "in_press": 3,
    "preprint": 2,
    "submitted": 2,
    "unknown": 1,
}

# 上游字段漂移：类型 / 版本标签 / 正式化标志
TYPE_KEYS = ("type", "work_type", "content_type", "publication_type")
VERSION_KEYS = ("version", "status", "publication_status", "state")
PUBLISHED_FLAGS = ("published", "is_published", "journal_ref", "published_doi")
PREPRINT_FLAGS = ("preprint", "is_preprint", "arxiv_id")


def _flag_value(record, key):
    if key not in record:
        return None
    value = record.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower() not in ("", "0", "false", "no")


def classify(record):
    """返回 ``(version, rank)``。纯函数，无副作用。"""
    labels = []
    for key in TYPE_KEYS + VERSION_KEYS:
        value = record.get(key)
        if value:
            labels.append(str(value).strip().lower())

    for label in labels:
        if label in ("published", "accepted", "in_press", "journal-article", "journal_article"):
            return "published", VERSION_RANK["published"]
        if label in ("preprint", "posted-content", "posted_content", "submitted"):
            return "preprint", VERSION_RANK["preprint"]

    if _flag_value(record, "published") or _flag_value(record, "is_published"):
        return "published", VERSION_RANK["published"]
    if record.get("journal_ref") or record.get("published_doi"):
        return "published", VERSION_RANK["published"]
    if _flag_value(record, "is_preprint") or record.get("preprint"):
        return "preprint", VERSION_RANK["preprint"]

    return "unknown", VERSION_RANK["unknown"]


def merge(old, new):
    """``store.upsert`` 的合并函数：正式版永不被预印本降级。"""
    old_rank = int(old.get("_version_rank") or 0)
    new_rank = int(new.get("_version_rank") or 0)
    if new_rank < old_rank:
        return old  # 保留已落库的高版本记录，重放不产生写入
    return new


def process(record):
    rec = dict(record)
    version, rank = classify(rec)
    rec["_version"] = version
    rec["_version_rank"] = rank
    return rec
