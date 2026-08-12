"""ci-diagnose 的 GitHub API 封装:workflow jobs / job 日志 / 找 PR / issue 评论。

与 pr-review 的 GitHubClient 独立(端点不同),复用 httpx 与错误处理模式。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import httpx

API_VERSION_HEADERS = {"X-GitHub-Api-Version": "2022-11-28"}


class GitHubError(Exception):
    """GitHub API 调用失败。"""


class GitHubClient:
    """ci-diagnose 需要的 GitHub API 最小面。"""

    def __init__(
        self,
        token: str,
        repo: str,  # owner/name
        base_url: str = "https://api.github.com",
    ):
        self.repo = repo
        self._headers = {**API_VERSION_HEADERS, "Authorization": f"Bearer {token}"}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=self._headers, timeout=60.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ workflow
    def get_workflow_jobs(self, run_id: int) -> list[dict]:
        """PR 目标 workflow run 的全部 job(含结论与 logs_url)。"""
        data = self._get(f"/repos/{self.repo}/actions/runs/{run_id}/jobs")
        return list(data.get("jobs", []))

    def download_job_logs(self, job_id: int) -> str:
        """下载单个 job 的日志(API 返回 zip,容错为纯文本)。"""
        resp = self._client.get(f"/repos/{self.repo}/actions/jobs/{job_id}/logs")
        if resp.status_code >= 400:
            raise GitHubError(
                f"GitHub API {resp.status_code} jobs/{job_id}/logs: {resp.text[:200]}"
            )
        raw = resp.content
        # 优先按 zip 解压(GitHub 官方日志端点返回压缩包)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                parts = [zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()]
            return "\n".join(parts)
        except zipfile.BadZipFile:
            return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ PR
    def find_pr_by_sha(self, sha: str) -> int | None:
        """按 head sha 找开放 PR;找不到返回 None。"""
        data = self._get(
            f"/repos/{self.repo}/commits/{sha}/pulls",
            params={"state": "open", "per_page": 5},
        )
        for pr in data or []:
            if pr.get("head", {}).get("sha") == sha:
                return int(pr["number"])
        return None

    def post_issue_comment(self, pr_number: int, body: str) -> dict:
        """在 PR 上发评论(issue comments 接口)。"""
        return self._post(
            f"/repos/{self.repo}/issues/{pr_number}/comments", {"body": body}
        )

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
            raise GitHubError(f"GitHub API {resp.status_code} {path}: {resp.text[:300]}")
        return resp.json()
