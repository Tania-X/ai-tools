"""GitHub API 封装(httpx): 开 PR / 轮询 check-run / 拉评论 / 关 PR / merge。"""

from __future__ import annotations

import time
from typing import Any

import httpx

_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class GitHubAPI:
    def __init__(self, token: str, repo: str):
        self.repo = repo  # owner/name
        self._client = httpx.Client(
            base_url="https://api.github.com",
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ PR
    def create_pr(self, base: str, head: str, title: str) -> dict:
        return self._post(f"/repos/{self.repo}/pulls", {"base": base, "head": head, "title": title})

    def close_pr(self, pr_number: int) -> dict:
        return self._patch(f"/repos/{self.repo}/pulls/{pr_number}", {"state": "closed"})

    def merge_pr(self, pr_number: int) -> dict:
        return self._put(f"/repos/{self.repo}/pulls/{pr_number}/merge", {"merge_method": "squash"})

    # ------------------------------------------------------------------ 轮询
    def poll_check_run(self, sha: str, name: str = "AI Review", timeout: int = 300, interval: int = 15) -> str | None:
        """轮询指定 check-run 直到终态, 返回 conclusion(success/failure/neutral) 或 None(超时)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            conclusion = self._get_check_conclusion(sha, name)
            if conclusion is not None:
                return conclusion
            time.sleep(interval)
        return None

    def _get_check_conclusion(self, sha: str, name: str) -> str | None:
        """查询 commit 的 check-runs, 返回 name 匹配且已完成的 conclusion。"""
        data = self._get(f"/repos/{self.repo}/commits/{sha}/check-runs", params={"per_page": 50})
        for run in data.get("check_runs", []):
            if run.get("name") == name and run.get("status") == "completed":
                return run.get("conclusion")
        return None

    # ------------------------------------------------------------------ 评论
    def list_issue_comments(self, pr_number: int) -> list[dict]:
        return list(self._get(f"/repos/{self.repo}/issues/{pr_number}/comments", params={"per_page": 100}))

    # ------------------------------------------------------------------ 内部
    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._client.get(path, params=params)
        return self._handle(resp, path)

    def _post(self, path: str, payload: dict) -> Any:
        resp = self._client.post(path, json=payload)
        return self._handle(resp, path)

    def _patch(self, path: str, payload: dict) -> Any:
        resp = self._client.patch(path, json=payload)
        return self._handle(resp, path)

    def _put(self, path: str, payload: dict) -> Any:
        resp = self._client.put(path, json=payload)
        return self._handle(resp, path)

    @staticmethod
    def _handle(resp: httpx.Response, path: str) -> Any:
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API {resp.status_code} {path}: {resp.text[:300]}")
        return resp.json()
