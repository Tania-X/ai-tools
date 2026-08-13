"""汇总报告生成(纯函数)。"""

from __future__ import annotations

from typing import Any


def render_report(results: list[dict[str, Any]], level: int) -> str:
    """把各 case 结果渲染成 Markdown 报告。"""
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
        lines.append(f"| {r['case']} | {status} | {note} |")

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
