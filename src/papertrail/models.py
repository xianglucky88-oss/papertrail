from dataclasses import dataclass, field
from typing import Any

@dataclass
class Record:
    key: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    """跨阶段共享的执行上下文。

    纯函数阶段只读 record；有状态阶段（author_id / version / dedup）
    通过 con 复用同一套键值持久化，从而支持提交中断后重放。
    """
    con: Any = None
    seq: int = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq
