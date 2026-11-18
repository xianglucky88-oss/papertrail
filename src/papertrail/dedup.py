"""单一职责：按规范键去重（dedup）。

去重键优先级：``doi_map`` 产出的规范 DOI 键 > 显式 id > 内容摘要。
同一批次里重复出现的论文只落一次；跨批次 / 跨来源的重复由
:func:`store.upsert` 的稳定主键再次兜底（同一键不会写出第二行）。
"""
from __future__ import annotations

import hashlib
import json

from . import doi_map

ID_KEYS = ("id", "ID", "arxiv_id", "external_id", "work_id")


def dedup_key(record):
    """稳定去重键。确定性、与字段顺序无关。"""
    canonical = record.get("_canonical_key") or doi_map.canonical_key(record)
    if canonical:
        return canonical
    for key in ID_KEYS:
        value = record.get(key)
        if value:
            return f"id:{value}"
    payload = json.dumps(
        record, sort_keys=True, ensure_ascii=False, default=str
    ).encode()
    return "sha:" + hashlib.sha256(payload).hexdigest()[:16]


def process(record):
    rec = dict(record)
    rec["_dedup_key"] = dedup_key(rec)
    rec["_stage_dedup"] = "ok"
    return rec


def dedup_records(records):
    """批内去重：保留每个键的 **最后** 一次出现（后到者可能更新）。

    返回 ``(unique_records, duplicate_count)``。``_dedup_key`` 必须已存在
    （即记录已过 :func:`process`）。
    """
    seen = {}
    order = []
    duplicates = 0
    for record in records:
        key = record.get("_dedup_key") or dedup_key(record)
        if key in seen:
            duplicates += 1
            seen[key] = record
        else:
            seen[key] = record
            order.append(key)
    return [seen[key] for key in order], duplicates
