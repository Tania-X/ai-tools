"""平台适配层接口: 让审查引擎不直接依赖 GitHub。

ReviewRunner 只依赖这里的 ReviewPlatform Protocol;
GitHubClient 是它的一个实现, 后续可新增 GitLab/Local/CLI 等适配器。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .types import PRInfo


@runtime_checkable
class ReviewPlatform(Protocol):
    """PR 审查平台需要提供的最小能力集。"""

    repo: str

    # 元信息与 diff
    def get_pr_info(self) -> PRInfo: ...
    def get_pr_files(self, per_page: int = 100) -> list[dict]: ...

    # 评论/门禁/线程(主要由入口层使用, 核心引擎按需使用)
    def post_review(
        self,
        body: str,
        *,
        head_sha: str,
        comments: list[dict] | None = None,
        event: str = "COMMENT",
    ) -> dict: ...
    def create_check_run(
        self,
        name: str,
        head_sha: str,
        conclusion: str,
        *,
        title: str = "",
        summary: str = "",
    ) -> dict: ...
    def count_ai_reviews(self) -> int: ...

    # 行内评论线程(决议驱动/回复)
    def get_pull_comments(self, per_page: int = 100) -> list[dict]: ...
    def post_pull_comment(self, body: str, *, in_reply_to: int) -> dict: ...
