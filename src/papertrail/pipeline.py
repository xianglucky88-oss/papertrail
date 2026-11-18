"""薄编排：loader → normalizer → harvest → author_id → doi_map → version → dedup → keyer → store。

只负责按顺序调用阶段，不含业务判断。有状态阶段共享同一个 Context（含 sqlite
连接），身份/版本/去重实体在每条记录处理后即提交，因此流程在任意一条之后中断，
重跑都能从持久化状态回放，且重复导入不改变既有身份。
"""
import importlib
from . import store as _store
from . import loader as _loader
from .keyer import key_of
from .models import Record, Context

CHAIN_NAMES = ['normalizer', 'harvest', 'author_id', 'doi_map', 'version', 'dedup']

def run(con=None, path=None, kind="generic", commit_each=True):
    own = con is None
    con = con or _store.connect()
    try:
        ctx = Context(con=con)
        chain = [importlib.import_module("." + n, __package__).process
                 for n in CHAIN_NAMES]
        fed = 0
        for raw in _loader.load(path=path, kind=kind):
            rec = raw
            for fn in chain:
                rec = fn(rec, ctx) if _takes_ctx(fn) else fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
            if commit_each:
                _store.commit(con)
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()

def _takes_ctx(fn):
    return fn.__code__.co_argcount >= 2 if hasattr(fn, "__code__") else False
