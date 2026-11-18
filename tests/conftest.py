"""测试夹具：把 doi_map 的进程级默认 mapper 换成内存实例，保证测试不落盘。

同时让流水线测试之间彼此隔离（每个测试拿到全新的标识映射状态）。
pytest 可用时通过 autouse fixture 自动生效；仅用 unittest 运行时，
:func:`in_memory_mapper` 可在 setUp 中手动调用。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from papertrail import doi_map  # noqa: E402

try:
    import pytest  # noqa: E402
except ImportError:  # 环境只有标准库时，退化为手动夹具
    pytest = None


def in_memory_mapper():
    """重置并注入一个全新的内存 mapper，返回该实例。"""
    doi_map.reset_default_mapper()
    mapper = doi_map.IdentifierMapper(":memory:")
    doi_map._default_mapper = mapper
    return mapper


if pytest is not None:
    @pytest.fixture(autouse=True)
    def _in_memory_doi_mapper():
        mapper = in_memory_mapper()
        yield mapper
        doi_map.reset_default_mapper()
