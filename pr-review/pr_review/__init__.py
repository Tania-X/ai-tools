"""PR AI 审查(pr_review)。

GitHub Action 自建 CodeRabbit 替代:
取 PR diff → 按文件切片 → 走 gateway 调 LLM → JSON 结构化输出 → 发 PR review 评论。
"""

from .config import DEFAULT_CONFIG, ReviewConfig, load_config
from .diff import FileDiff, parse_diff
from .github import GitHubClient

__all__ = [
    "DEFAULT_CONFIG",
    "ReviewConfig",
    "load_config",
    "FileDiff",
    "parse_diff",
    "GitHubClient",
]
