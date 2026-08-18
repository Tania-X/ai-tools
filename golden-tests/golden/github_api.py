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

    def create_pr_or_reuse(self, base: str, head: str, title: str) -> dict:
        """创建 PR; 若 head 已有 open PR(崩溃循环残留)则关闭后重建(2026-08-18 422 根因)。"""
        try:
            return self.create_pr(base, head, title)
        except RuntimeError as e:
            if "already exists" not in str(e):
                raise
            existing = self._find_open_pr(head)
            if existing:
                self.close_pr(existing["number"])
            return self.create_pr(base, head, title)

    def _find_open_pr(self, head: str) -> dict | None:
        prs = self._get(f"/repos/{self.repo}/pulls", params={"state": "open", "head": f"{self.repo.split('/')[0]}:{head}", "per_page": 10})
        return prs[0] if prs else None

    def delete_branch(self, branch: str) -> dict:
        """API 删分支(不依赖 git 凭据; git push --delete 在沙箱拦凭据写时会残留)。"""
        return self._delete(f"/repos/{self.repo}/git/refs/heads/{branch}")

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

    def list_pull_reviews(self, pr_number: int) -> list[dict]:
        """PR review 列表(整体评论通道)。

        pr-review 的整体评论通过 POST /pulls/{n}/reviews 发布(review body),
        不是 issue comments——驱动器必须查此通道才能拿到 AI 审查结果。
        """
        return list(self._get(f"/repos/{self.repo}/pulls/{pr_number}/reviews", params={"per_page": 100}))

    # ------------------------------------------------------------------ 内部
    # 网络类异常(非业务错误): 用户网络时好时坏(2026-08-14 回归崩溃根因), 统一重试
    _NETWORK_ERRORS = (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError)
    _MAX_NETWORK_RETRIES = 4

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._retry(lambda: self._client.get(path, params=params), path)

    def _post(self, path: str, payload: dict) -> Any:
        return self._retry(lambda: self._client.post(path, json=payload), path)

    def _patch(self, path: str, payload: dict) -> Any:
        return self._retry(lambda: self._client.patch(path, json=payload), path)

    def _put(self, path: str, payload: dict) -> Any:
        return self._retry(lambda: self._client.put(path, json=payload), path)

    def _delete(self, path: str) -> Any:
        return self._retry(lambda: self._client.delete(path), path)

    def _retry(self, fn, path: str) -> Any:
        last_err: Exception | None = None
        for attempt in range(self._MAX_NETWORK_RETRIES):
            try:
                return self._handle(fn(), path)
            except self._NETWORK_ERRORS as e:
                last_err = e
                print(f"  网络异常(第{attempt + 1}/{self._MAX_NETWORK_RETRIES}次): {type(e).__name__}, 重试 {path}")
                time.sleep(3)
        raise last_err  # 重试耗尽, 由上层(场景 skip)兜底

    @staticmethod
    def _handle(resp: httpx.Response, path: str) -> Any:
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API {resp.status_code} {path}: {resp.text[:300]}")
        return resp.json()
