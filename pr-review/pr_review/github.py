"""GitHub API 轻量客户端(httpx, 无第三方 SDK)。

需要的权限:GITHUB_TOKEN 具备 pull-requests: write 即可(pull_request 事件默认有)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

API_VERSION_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


@dataclass
class PRInfo:
    """PR 元信息(供 prompt 上下文使用)。"""

    number: int
    title: str
    body: str
    head_sha: str
    head_ref: str
    base_ref: str


class GitHubError(Exception):
    """GitHub API 调用失败。"""


class GitHubClient:
    """只封装 pr-review 需要的 4 个端点,保持最小面。"""

    def __init__(
        self,
        token: str,
        repo: str,  # owner/name
        pr_number: int,
        base_url: str = "https://api.github.com",
    ):
        self.repo = repo
        self.pr_number = pr_number
        self._headers = {**API_VERSION_HEADERS, "Authorization": f"Bearer {token}"}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=self._headers)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ PR 元信息
    def get_pr_info(self) -> PRInfo:
        data = self._get(f"/repos/{self.repo}/pulls/{self.pr_number}")
        return PRInfo(
            number=self.pr_number,
            title=data.get("title", ""),
            body=data.get("body") or "",
            head_sha=data["head"]["sha"],
            head_ref=data["head"]["ref"],
            base_ref=data["base"]["ref"],
        )

    # ------------------------------------------------------------------ 文件与 diff
    def get_pr_files(self, per_page: int = 100) -> list[dict]:
        """分页取 PR 文件列表(每项含 filename/status/patch/additions/deletions)。"""
        files: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                f"/repos/{self.repo}/pulls/{self.pr_number}/files",
                params={"per_page": per_page, "page": page},
            )
            files.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return files

    # ------------------------------------------------------------------ 发评论
    def post_review(
        self,
        body: str,
        *,
        head_sha: str,
        comments: list[dict] | None = None,
        event: str = "COMMENT",
    ) -> dict:
        """提交一条 review 评论。

        comments: 行内评论列表 [{path, line, side, body}],初版传 None 只发整体评论。
        """
        payload: dict[str, Any] = {"body": body, "event": event, "commit_id": head_sha}
        if comments:
            payload["comments"] = comments
        return self._post(f"/repos/{self.repo}/pulls/{self.pr_number}/reviews", payload)

    # ------------------------------------------------------------------ check-run
    def create_check_run(
        self,
        name: str,
        head_sha: str,
        conclusion: str,  # success / failure / neutral / skipped ...
        *,
        title: str = "",
        summary: str = "",
    ) -> dict:
        """创建/更新 check-run,供分支保护规则做合并门禁(需 checks: write 权限)。

        conclusion 取值参考:
            success   通过(未达到门槛)
            failure   未通过(存在达到门槛的问题, PR 显示红)
            neutral   不阻塞(仅提示)
        """
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
        }
        if title or summary:
            payload["output"] = {"title": title, "summary": summary}
        return self._post(f"/repos/{self.repo}/check-runs", payload)

    # ------------------------------------------------------------------ 内部
    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._client.get(path, params=params)
        return self._handle(resp, path)

    def _post(self, path: str, payload: dict) -> Any:
        resp = self._client.post(path, json=payload)
        return self._handle(resp, path)

    @staticmethod
    def _handle(resp: httpx.Response, path: str) -> Any:
        if resp.status_code >= 400:
            raise GitHubError(
                f"GitHub API {resp.status_code} {path}: {resp.text[:300]}"
            )
        return resp.json()
