"""GitHub API 轻量客户端(httpx, 无第三方 SDK)。

需要的权限:GITHUB_TOKEN 具备 pull-requests: write 即可(pull_request 事件默认有)。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import PRFile, PRInfo

logger = logging.getLogger(__name__)

API_VERSION_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# 我们发布的 review 评论标识(用于统计"第 N 次评审")
REVIEW_MARKER = "🤖 AI 代码审查"


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
    def get_pr_files(self, per_page: int = 100) -> list[PRFile]:
        """分页取 PR 文件列表, 并转换为平台无关的 PRFile。"""
        files: list[PRFile] = []
        page = 1
        while True:
            batch = self._get(
                f"/repos/{self.repo}/pulls/{self.pr_number}/files",
                params={"per_page": per_page, "page": page},
            )
            files.extend(
                PRFile(
                    filename=item.get("filename", ""),
                    status=item.get("status", "modified"),
                    patch=item.get("patch", ""),
                    previous_filename=item.get("previous_filename", ""),
                )
                for item in batch
            )
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

    # ------------------------------------------------------------------ 评审次数
    def count_ai_reviews(self) -> int:
        """统计该 PR 上已发布的 AI review 条数(按 REVIEW_MARKER 过滤 body)。

        用于显示"第 N 次评审":本次次数 = count + 1。
        只数我们发的(review body 含固定标识),不影响用户手动发的 review。
        """
        count = 0
        page = 1
        while True:
            batch = self._get(
                f"/repos/{self.repo}/pulls/{self.pr_number}/reviews",
                params={"per_page": 100, "page": page},
            )
            count += sum(1 for r in batch if REVIEW_MARKER in (r.get("body") or ""))
            if len(batch) < 100:
                break
            page += 1
        return count

    # ------------------------------------------------------------------ 行内评论线程(交互)
    def get_pull_comments(self, per_page: int = 100) -> list[dict]:
        """分页拉取 PR 全部行内评论(review comments, 含 in_reply_to_id 线程关系)。"""
        comments: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                f"/repos/{self.repo}/pulls/{self.pr_number}/comments",
                params={"per_page": per_page, "page": page},
            )
            comments.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return comments

    def post_pull_comment(self, body: str, *, in_reply_to: int) -> dict:
        """在行内评论线程里回复(in_reply_to 指向被回复的评论 id, 形成对话线程)。

        回复已有评论时无需 path/line,位置继承原评论。
        """
        payload: dict[str, Any] = {"body": body, "in_reply_to": in_reply_to}
        return self._post(f"/repos/{self.repo}/pulls/{self.pr_number}/comments", payload)

    # ------------------------------------------------------------------ 内部
    # ------------------------------------------------------------------ CI 结论(P0-5)
    def get_check_runs(self, head_sha: str) -> list[dict]:
        """取该 commit 的 check-runs(只保留结论, 供"机器已确认"注入)。

        容错: 无权限(缺 checks:read)或接口失败 → 返回 [], 审查照常进行
        (注入工具链事实是增强项, 不能因为拿不到就把整轮评审搞挂)。
        """
        if not head_sha:
            return []
        # 分页(评审 R1-3): 单页上限 100, 超过会把声明在后面的工具链 check 漏掉 →
        # 该维度被当成"未知"而不注入。与 get_pull_comments 同一套写法。
        runs: list[dict] = []
        page = 1
        while True:
            try:
                data = self._get(
                    f"/repos/{self.repo}/commits/{head_sha}/check-runs",
                    params={"per_page": 100, "page": page},
                )
            except Exception as e:  # noqa: BLE001 增强项失败不影响主流程
                logger.warning("获取 check-runs 失败(忽略, 不做工具链注入): %s", e)
                return []
            batch = data.get("check_runs") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                return []
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 20:  # 兜底: 极端仓库不至于无限翻页
                logger.warning("check-runs 分页超过 20 页, 停止拉取")
                break
        return [
            {
                "name": str(r.get("name", "")),
                # conclusion 只放 conclusion(评审 R4-2): 早先回退到 status, 会把
                # queued/in_progress 塞进"结论"字段, 语义被污染(下游按 conclusion 判断即踩坑)
                "conclusion": str(r.get("conclusion") or ""),
                "status": str(r.get("status", "")),
            }
            for r in runs
            if isinstance(r, dict)
        ]

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
