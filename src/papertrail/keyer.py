"""单一职责：派生稳定主键。

通用记录沿用输入 id / 内容哈希；论文记录优先使用 dedup 阶段基于
canonical 作者身份与版本链算出的指纹键，保证身份合并后键可回放地变化。
"""
import hashlib, json

def key_of(record):
    if isinstance(record.get("dedup"), dict) and record["dedup"].get("key"):
        return record["dedup"]["key"]
    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
