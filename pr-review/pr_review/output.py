"""平台无关的审查输出/门禁格式化逻辑。

从 main.py 抽出, 便于 CLI/本地/其他平台复用, 不依赖 GitHub。
"""

from __future__ import annotations

from .config import ReviewConfig
from .review import ReviewResult, SEVERITY_META


def severity_label(sev: int) -> str:
    """数字级别 → 展示名(1建议~5致命)。"""
    return SEVERITY_META.get(sev, SEVERITY_META[2])["name"]


def has_blocking_issues(cfg: ReviewConfig, result: ReviewResult) -> bool:
    """是否达到合并门禁(fail_on_severity)级别的问题。

    needs_review=true 的问题(设计意图类不确定判断)不计入门禁——避免误报阻塞合并。
    fail_on_severity 0 = 永不拦(只发评论)。
    """
    threshold = cfg.fail_on_severity
    if threshold <= 0:
        return False
    return any(
        not issue.needs_review and issue.severity >= threshold
        for issue in result.issues
    )


def check_title(result: ReviewResult, blocked: bool, cfg: ReviewConfig) -> str:
    prefix = f"[第{result.review_no}次] " if result.review_no else ""
    if blocked:
        counts = result.severity_counts
        parts = ", ".join(
            f"{c} {severity_label(s)}" for s, c in sorted(counts.items(), reverse=True)
        )
        return f"{prefix}存在达到门禁级别({cfg.fail_on_severity})的问题: {parts}"
    if result.has_issues:
        return f"{prefix}审查通过(未达到门禁级别)"
    return f"{prefix}审查通过,未发现问题"


def check_summary(result: ReviewResult, cfg: ReviewConfig) -> str:
    counts = result.severity_counts
    parts = ", ".join(f"{severity_label(s)} {counts[s]}" for s in sorted(counts, reverse=True))
    lines = [
        f"- 问题统计: {parts}",
        f"- 门禁线: {cfg.fail_on_severity}({severity_label(cfg.fail_on_severity) if cfg.fail_on_severity else '不拦'})",
        f"- 必修线: {cfg.require_fix_severity}({severity_label(cfg.require_fix_severity)})",
    ]
    if result.quality_score is not None:
        lines.append(f"- 质量评分: {result.quality_score:.0f}/100")
    if result.skipped_files:
        lines.append(f"- 跳过文件: {result.skipped_files}")
    return "\n".join(lines)
