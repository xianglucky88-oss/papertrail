"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。"""
import json
from .config import FIXTURES, PAPER_FIXTURES

def load(path=None, kind="generic"):
    target = path or (PAPER_FIXTURES if kind == "papers" else FIXTURES)
    with open(target, encoding="utf-8") as f:
        return json.load(f)
