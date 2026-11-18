"""薄编排：loader → normalizer → harvest → doi_map → version → dedup → keyer → store。

不含业务判断：每个阶段只调用对应模块，采集的恢复点语义由 :mod:`harvest`
与 :mod:`store` 共同保证。

两种入口：
* :func:`run` —— 旧行为，读 fixtures 的扁平记录逐条过链（兼容旧采集输入）；
* :func:`run_sources` —— 多来源分页采集，游标以提交确认为准。
"""
import importlib

from . import dedup as _dedup
from . import harvest as _harvest
from . import loader as _loader
from . import store as _store
from . import version as _version
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ["normalizer", "harvest", "doi_map", "version", "dedup"]


def _chain():
    chain = []
    for name in CHAIN_NAMES:
        module = importlib.import_module("." + name, __package__)
        chain.append(module.process)
    return chain


def prepare_items(items, ctx):
    """把一页原始条目变成 ``Record`` 列表（供 :mod:`harvest` 调用）。

    顺序：注入来源/分页上下文 → 逐阶段变换 → 批内去重 → 主键。
    返回的列表已按规范键去重，同一批次重放内容完全一致。
    """
    chain = _chain()
    prepared = []
    for item in items:
        rec = _harvest.enrich(item, ctx)
        for fn in chain:
            rec = fn(rec)
        prepared.append(rec)

    unique, _duplicates = _dedup.dedup_records(prepared)
    return [Record(key=key_of(rec), data=rec) for rec in unique]


def run(con=None):
    """旧入口：逐条处理扁平 fixtures，保持原有行为与幂等性。"""
    own = con is None
    con = con or _store.connect()
    try:
        chain = _chain()
        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec), merge=_version.merge)
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()


def run_sources(sources, *, con=None, db_path=None, merge=None,
                cache=None, clock=None, sleep=None, max_workers=1):
    """多来源采集入口。单个来源失败不影响其他来源。"""
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    return _harvest.harvest_sources(
        sources,
        prepare_items,
        con=con,
        db_path=db_path,
        merge=merge if merge is not None else _version.merge,
        cache=cache,
        max_workers=max_workers,
        **kwargs,
    )
