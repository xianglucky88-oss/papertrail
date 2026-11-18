"""单一职责：派生稳定主键。

优先级：``doi_map`` 规范键 > 显式 id > ``dedup`` 去重键 > 内容摘要。
同一 DOI 无论来自哪个来源、写成什么形式，都落在同一行；旧采集输入
（只有 id）仍沿用原来的 ``id`` 作为主键，行为不变。
"""
import hashlib
import json

from . import dedup as _dedup
from . import doi_map as _doi_map

ID_KEYS = ("id", "ID", "arxiv_id", "external_id", "work_id")


def key_of(record):
    canonical = record.get("_canonical_key") or _doi_map.canonical_key(record)
    if canonical:
        return canonical
    for key in ID_KEYS:
        value = record.get(key)
        if value:
            return str(value)
    dedup_key = record.get("_dedup_key") or _dedup.dedup_key(record)
    if dedup_key:
        return dedup_key
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]
