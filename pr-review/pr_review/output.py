"""平台无关的审查输出/门禁格式化逻辑。

从 main.py 抽出, 便于 CLI/本地/其他平台复用, 不依赖 GitHub。
"""

from __future__ import annotations

from .config import ReviewConfig
from .review import ReviewResult, SEVERITY_META


def severity_label(sev: int) -> str:
    """数字级别 → 展示名(1建议~5致命)。"""
    return SEVERITY_META.get(sev, SEVERITY_META[2])["name"]


def split_counts(result: ReviewResult) -> tuple[dict[int, int], dict[int, int]]:
    """按“是否计入门禁”拆分问题计数: (计入门禁的, 仅需人工确认的)。

    门禁只看 `not issue.needs_review` 的问题;但标题/统计若把两类混在一起, 就会出现
    “标题写通过、统计却有严重 1” 的自相矛盾(2026-09-11 真实误判: 一条被标记
    “⚠️ 需人工确认” 的严重级问题不计入门禁, 统计却仍显示“严重 1”)。
    """
    blocking: dict[int, int] = {}
    manual: dict[int, int] = {}
    for issue in result.issues:
        bucket = manual if issue.needs_review else blocking
        bucket[issue.severity] = bucket.get(issue.severity, 0) + 1
    return blocking, manual


def _format_counts(counts: dict[int, int]) -> str:
    return ", ".join(
        f"{counts[s]} {severity_label(s)}" for s in sorted(counts, reverse=True)
    )


def check_run_payload(
    result: ReviewResult,
    blocked: bool,
    cfg: ReviewConfig,
    *,
    judge_failed: bool = False,
) -> tuple[str, str]:
    """check-run 的 (title, summary)。

    judge 故障只在**标题**加标记: 正文里的解释由 check_summary 单一来源给出
    (2026-09-13 评审 R1-3: 原先 main.py 又拼了一段同类文字, 同一事实在 check 里出现两次)。
    """
    title = check_title(result, blocked, cfg)
    summary = check_summary(result, cfg)
    if judge_failed:
        title += " · judge 故障"
    return title, summary


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
    blocking, manual = split_counts(result)
    if blocked:
        # Count only what actually tripped the gate, so the title matches the verdict.
        return (
            f"{prefix}存在达到门禁级别({cfg.fail_on_severity})的问题: "
            f"{_format_counts(blocking)}"
        )
    if result.has_issues:
        # Say WHY the gate passed: issues that need a human are excluded by design.
        reason = _format_counts(blocking) or "无"
        if manual:
            return (
                f"{prefix}审查通过(未达到门禁级别): 计入门禁 {reason}; "
                f"需人工确认(不计入门禁) {_format_counts(manual)}"
            )
        return f"{prefix}审查通过(未达到门禁级别): 计入门禁 {reason}"
    return f"{prefix}审查通过,未发现问题"


def check_summary(result: ReviewResult, cfg: ReviewConfig) -> str:
    blocking, manual = split_counts(result)
    parts_list: list[str] = []
    if blocking:
        parts_list.append(
            ", ".join(f"{severity_label(s)} {blocking[s]}" for s in sorted(blocking, reverse=True))
        )
    if manual:
        parts_list.append(
            ", ".join(f"{severity_label(s)} {manual[s]}(需人工确认)" for s in sorted(manual, reverse=True))
        )
    parts = ", ".join(parts_list)
    lines = [
        f"- 问题统计: {parts or '无'}",
        f"- 计入门禁: {_format_counts(blocking) or '无'}",
        f"- 门禁线: {cfg.fail_on_severity}({severity_label(cfg.fail_on_severity) if cfg.fail_on_severity else '不拦'})",
        f"- 必修线: {cfg.require_fix_severity}({severity_label(cfg.require_fix_severity)})",
    ]
    if manual:
        lines.append("- 说明: 「需人工确认」的问题按仓库约定不计入门禁, 不会阻塞合并")
    if result.quality_score is not None:
        lines.append(f"- 质量评分: {result.quality_score:.0f}/100")
    elif getattr(result, "quality_parse_failed", False):
        lines.append("- 质量评分: 不可用(judge 输出无法解析, 工具故障; 未做质量评估)")
        lines.append("- 说明: 按「评分与门禁解耦」, 本轮 issue 门禁判定不受评审故障影响")
    if result.skipped_files:
        lines.append(f"- 跳过文件: {result.skipped_files}")
    return "\n".join(lines)
