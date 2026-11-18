"""薄编排：loader → normalizer → harvest → version → doi_map → dedup → keyer → store。

不含业务判断：version 阶段需要跨记录的持久化状态，因此整条流水线在
version.session(con) 内运行——正常退出统一 finalize 版本链并提交，
半批失败时由 session 回滚到批次开始前的一致状态。
"""
import importlib

from . import store as _store
from . import loader as _loader
from . import version
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'harvest', 'version', 'doi_map', 'dedup']


def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        with version.session(con):
            chain = [importlib.import_module("." + n, __package__).process for n in CHAIN_NAMES]
            fed = 0
            for raw in _loader.load():
                rec = raw
                for fn in chain:
                    rec = fn(rec)
                _store.upsert(con, Record(key=key_of(rec), data=rec))
                fed += 1
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()
