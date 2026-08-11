"""审查编排:取 PR 文件 → 过滤/切片 → 调 LLM → 解析 → 发评论。

与 CodeRabbit 的差异:初版发整体 review 评论(JSON 结构含文件+行号),
行内评论作为后续增强,post_review 已预留 comments 参数。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gateway import ChatResponse, LLMClient

from .config import ReviewConfig
from .diff import DiffHunk, FileDiff, parse_diff
from .github import GitHubClient, PRInfo
from .prompt import build_messages, parse_review_json

logger = logging.getLogger("pr_review")

SEVERITY_ICONS = {"error": "🔴", "warn": "🟡", "info": "🔵"}


@dataclass
class ReviewIssue:
    file: str
    line: int
    severity: str
    title: str
    detail: str
    suggestion: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewIssue":
        return cls(
            file=str(d.get("file", "")),
            line=int(d.get("line", 0) or 0),
            severity=str(d.get("severity", "info")).lower(),
            title=str(d.get("title", "")),
            detail=str(d.get("detail", "")),
            suggestion=str(d.get("suggestion", "")),
        )


@dataclass
class ReviewResult:
    """一轮(或一批)审查的结果汇总。"""

    batches: int = 0
    issues: list[ReviewIssue] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    total_tokens: int = 0
    model: str = ""
    skipped_files: int = 0

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


class ReviewRunner:
    """pr-review 主流程。按批次切片,每批独立走 LLM,最后汇总发一条评论。"""

    def __init__(
        self,
        github: GitHubClient,
        llm: LLMClient,
        config: ReviewConfig,
        *,
        max_retry_bad_json: int = 1,
    ):
        self.github = github
        self.llm = llm
        self.config = config
        self.max_retry_bad_json = max_retry_bad_json

    # ------------------------------------------------------------------ 入口
    def run(self) -> ReviewResult:
        pr = self.github.get_pr_info()
        raw_files = self.github.get_pr_files()

        candidates: list[FileDiff] = []
        skipped = 0
        for item in raw_files:
            path = item.get("filename", "")
            if self.config.should_ignore(path):
                skipped += 1
                continue
            # 没有 patch 的(超大文件/二进制)跳过
            patch = item.get("patch", "")
            if not patch:
                skipped += 1
                continue
            candidates.append(
                FileDiff(
                    path=path,
                    old_path=item.get("previous_filename", ""),
                    status=item.get("status", "modified"),
                    hunks=parse_diff(patch)[0].hunks if parse_diff(patch) else [],
                )
            )

        # 单文件超长:截断 hunk 行,避免单次 prompt 爆 token
        for fd in candidates:
            self._truncate_if_too_long(fd)

        if not candidates:
            logger.info("没有可审查的文件(全部被忽略或无 diff)")
            return ReviewResult(skipped_files=skipped)

        result = ReviewResult(model=self.llm.config.get().model, skipped_files=skipped)
        batches = self._batch_files(candidates)
        result.batches = len(batches)

        for idx, batch in enumerate(batches, start=1):
            resp = self._review_batch(pr, batch, idx, len(batches))
            result.summaries.append(self._extract_summary(resp.content))
            result.issues.extend(self._extract_issues(resp.content))
            result.total_cost += resp.cost
            result.total_tokens += resp.prompt_tokens + resp.completion_tokens

        result.issues.sort(key=lambda i: (i.file, i.line))
        return result

    # ------------------------------------------------------------------ 单批审查
    def _review_batch(
        self, pr: PRInfo, batch: list[FileDiff], batch_no: int, batch_total: int
    ) -> ChatResponse:
        messages = build_messages(pr, batch, self.config, batch_no, batch_total)
        last_err: Exception | None = None
        for _ in range(self.max_retry_bad_json + 1):
            try:
                return self.llm.chat(messages)
            except ValueError as e:  # JSON 解析失败:重试一次
                last_err = e
                logger.warning("LLM 输出非 JSON,重试: %s", e)
        raise RuntimeError(f"LLM 多次返回非法 JSON,放弃: {last_err}")

    def _extract_summary(self, content: str) -> str:
        """从 JSON 中提取整体判断(失败时返回空串,不把原始 JSON 贴进评论)。"""
        try:
            data = parse_review_json(content)
            return str(data.get("summary", "")).strip()
        except ValueError:
            return ""

    def _extract_issues(self, content: str) -> list[ReviewIssue]:
        try:
            data = parse_review_json(content)
        except ValueError as e:
            logger.warning("解析审查结果失败: %s", e)
            return []
        issues: list[ReviewIssue] = []
        for item in data.get("issues", []) or []:
            issue = ReviewIssue.from_dict(item)
            if not issue.title or not issue.file:
                continue  # 缺关键字段的直接丢弃
            if not self.config.passes_filter(issue.severity):
                continue
            issues.append(issue)
        return issues

    # ------------------------------------------------------------------ 切片
    def _batch_files(self, files: list[FileDiff]) -> list[list[FileDiff]]:
        n = self.config.max_files_per_batch
        return [files[i : i + n] for i in range(0, len(files), n)]

    @staticmethod
    def _truncate_if_too_long(fd: FileDiff, max_lines: int = 800) -> None:
        """hunk 行数超过上限时截断保留前半段(保留下一条注释说明)。"""
        total = sum(len(h.lines) for h in fd.hunks)
        if total <= max_lines:
            return
        kept, budget = [], max_lines
        for hunk in fd.hunks:
            if budget <= 0:
                break
            take = min(len(hunk.lines), budget)
            kept.append(DiffHunk(old_start=hunk.old_start, new_start=hunk.new_start, lines=hunk.lines[:take]))
            budget -= take
        kept[-1].lines.append(f"// ... 文件过大,共 {total} 行 diff,仅展示前 {max_lines} 行")
        fd.hunks = kept

    # ------------------------------------------------------------------ 评论生成
    def format_comment(self, result: ReviewResult) -> str:
        """把审查结果渲染成 PR 评论 Markdown。"""
        lines: list[str] = [
            "## 🤖 AI 代码审查",
            "",
        ]
        if result.summaries:
            lines.append("**整体判断**:")
            for s in result.summaries:
                lines.append(f"- {s.strip()}")
            lines.append("")

        by_severity: dict[str, list[ReviewIssue]] = {}
        for issue in result.issues:
            by_severity.setdefault(issue.severity, []).append(issue)

        for sev in ("error", "warn", "info"):
            items = by_severity.get(sev, [])
            if not items:
                continue
            lines.append(f"### {SEVERITY_ICONS[sev]} {sev.capitalize()} ({len(items)})")
            for i in items:
                loc = f"`{i.file}`:{i.line}" if i.line else f"`{i.file}`"
                lines.append(f"- **{loc}** — {i.title}")
                if i.detail:
                    lines.append(f"  - {i.detail}")
                if i.suggestion:
                    lines.append(f"  - 💡 {i.suggestion}")
            lines.append("")

        if not result.issues:
            lines.append("未发现达到审查门槛的问题 ✅")
            lines.append("")

        stats = [
            f"模型: {result.model or '-'}",
            f"批次数: {result.batches}",
            f"token: {result.total_tokens}",
        ]
        if result.total_cost:
            stats.append(f"成本: ¥{result.total_cost:.4f}")
        if result.skipped_files:
            stats.append(f"跳过文件: {result.skipped_files}")
        if self.config.show_stats:
            lines.append("---")
            lines.append("_自动生成 · " + " · ".join(stats) + "_")
        return "\n".join(lines)
