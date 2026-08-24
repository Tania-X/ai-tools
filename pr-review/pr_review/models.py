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


@dataclass
class PRFile:
    """PR 中一个变更文件(平台无关表示)。

    由平台适配器负责把 GitLab/GitHub/本地 Git 的原始数据转换成该类型。
    """

    filename: str
    status: str = "modified"
    patch: str = ""
    previous_filename: str = ""
