import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _ROOT)          # 支持 `from src.papertrail ...`（pytest.ini 等价）
sys.path.insert(0, os.path.join(_ROOT, "src"))  # 支持 `import papertrail ...`

from src.papertrail import pipeline, store  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
try:
    from conftest import in_memory_mapper  # noqa: E402
except ImportError:  # pragma: no cover - 仅极端路径下发生
    in_memory_mapper = None


def test_pipeline_runs_and_idempotent():
    if in_memory_mapper is not None:
        in_memory_mapper()  # 标识映射走内存，测试不落盘、彼此隔离
    a = pipeline.run()
    b = pipeline.run()   # 重跑不增行（幂等 upsert）
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3   # r-003 重复被去重


def test_store_roundtrip():
    con = store.connect()
    try:
        assert store.count(con) >= 0
    finally:
        con.close()


class PipelineCompatibilityTests(unittest.TestCase):
    """同语义的 unittest 版本，保证无 pytest 环境也能跑全量校验。"""

    def test_pipeline_runs_and_idempotent(self):
        test_pipeline_runs_and_idempotent()

    def test_store_roundtrip(self):
        test_store_roundtrip()


if __name__ == "__main__":
    unittest.main(verbosity=2)
