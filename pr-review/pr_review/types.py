"""平台无关的数据类型(审查引擎核心不依赖 GitHub)。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PRInfo:
    """PR 元信息(供 prompt 上下文使用)。"""

    number: int
    title: str
    body: str
    head_sha: str
    head_ref: str
    base_ref: str
