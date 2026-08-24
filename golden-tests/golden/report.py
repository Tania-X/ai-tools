"""汇总报告生成(纯函数)。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def render_report(results: list[dict[str, Any]], level: int) -> str:
    """把各 case 结果渲染成 Markdown 报告(场景矩阵 + 能力维度聚合)。"""
    passed = [r for r in results if r.get("pass")]
    failed = [r for r in results if not r.get("pass") and r.get("status") != "skip"]
    skipped = [r for r in results if r.get("status") == "skip"]

    lines = [
        f"# Golden 测试报告(Level {level})",
        "",
        f"- 通过: {len(passed)} / {len(results)}",
        f"- 失败: {len(failed)}",
        f"- 跳过: {len(skipped)}",
        "",
        "## 场景矩阵",
        "",
        "| case | 结果 | 说明 |",
        "|------|------|------|",
    ]
    for r in results:
        status = "skip" if r.get("status") == "skip" else ("✅ pass" if r.get("pass") else "❌ fail")
        note = ""
        if status == "skip":
            note = r.get("reason", "跳过")
        elif r.get("pass"):
            a = r.get("actual", {})
            note = f"issues={a.get('total')}, check={r.get('check_conclusion')}"
        else:
            note = "; ".join(r.get("failures", []))
        cap = r.get("capability", "")
        lines.append(f"| {r['case']} | {status} | {note} |")

    # 能力维度聚合(2026-08-24): 引擎在各审查能力上的通过率, 而非只看单 case
    lines += ["", "## 能力维度", "", "| capability | 通过 | 总数 | 通过率 |", "|------|------|------|------|"]
    by_cap: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        if r.get("status") == "skip":
            continue
        by_cap[r.get("capability") or "unknown"].append(bool(r.get("pass")))
    for cap in sorted(by_cap):
        passes = by_cap[cap]
        n = len(passes)
        p = sum(passes)
        rate = f"{p / n * 100:.0f}%" if n else "-"
        lines.append(f"| {cap} | {p} | {n} | {rate} |")

    if failed:
        lines += ["", "## 失败详情", ""]
        for r in failed:
            lines.append(f"### {r['case']}")
            lines.append(f"- 期望: `{r.get('expected', {})}`")
            lines.append(f"- 实际: `{r.get('actual', {})}`")
            for f in r.get("failures", []):
                lines.append(f"  - {f}")
            lines.append("")

    lines.append("")
    lines.append(f"_生成时间: {__import__('datetime').datetime.now().isoformat(timespec='seconds')}_")
    return "\n".join(lines)
