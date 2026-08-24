"""本地 Git 平台适配器: 不依赖 GitHub, 用于 CLI / 本地验证。

ReviewPlatform 的一个最小实现:
- get_pr_files 基于 `git diff base head`
- 评论/check-run 等能力在本地降级为 stdout 输出
- 适合验证审查引擎可以脱离 GitHub 独立运行
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import PRFile, PRInfo


class LocalPlatform:
    """把本地 Git 仓库的两个 rev 之间的 diff 当作一次“PR”审查。"""

    def __init__(
        self,
        repo_root: str | Path = ".",
        *,
        base: str = "HEAD~1",
        head: str = "HEAD",
    ):
        self.repo_root = Path(repo_root).resolve()
        self.base = base
        self.head = head
        self.repo = str(self.repo_root)

    # ------------------------------------------------------------------ git 辅助
    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout

    # ------------------------------------------------------------------ ReviewPlatform
    def get_pr_info(self) -> PRInfo:
        head_sha = self._git("rev-parse", self.head).strip()
        return PRInfo(
            number=0,
            title=f"local {self.base}..{self.head}",
            body="",
            head_sha=head_sha,
            head_ref=self.head,
            base_ref=self.base,
        )

    def get_pr_files(self, per_page: int = 100) -> list[PRFile]:
        raw = self._git("diff", "--name-status", self.base, self.head)
        files: list[PRFile] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            filename = parts[-1]
            previous_filename = parts[1] if status.startswith("R") and len(parts) >= 3 else ""
            patch = self._git("diff", "--no-color", "--unified=3", self.base, self.head, "--", filename)
            files.append(
                PRFile(
                    filename=filename,
                    status=status,
                    patch=patch,
                    previous_filename=previous_filename,
                )
            )
        return files

    def get_pull_comments(self, per_page: int = 100) -> list[dict]:
        return []

    def post_review(
        self,
        body: str,
        *,
        head_sha: str,
        comments: list[dict] | None = None,
        event: str = "COMMENT",
    ) -> dict:
        print(f"===== LOCAL REVIEW ({head_sha}) =====")
        print(body)
        if comments:
            print(f"\n[inline comments: {len(comments)}]")
        return {}

    def create_check_run(
        self,
        name: str,
        head_sha: str,
        conclusion: str,
        *,
        title: str = "",
        summary: str = "",
    ) -> dict:
        print(f"[check-run] {name}: {conclusion} — {title}")
        if summary:
            print(summary)
        return {}

    def count_ai_reviews(self) -> int:
        return 0

    def post_pull_comment(self, body: str, *, in_reply_to: int) -> dict:
        print(f"[reply to {in_reply_to}] {body}")
        return {}
