"""诊断 prompt 与评论格式。"""

from __future__ import annotations

DIAGNOSE_SYSTEM_PROMPT = """你是 CI 故障诊断专家,根据 CI 失败日志片段定位问题并给出修复建议。
要求:
- 只依据日志给出结论;信息不足时明确说明"日志片段不足",不要猜测
- summary: 一句话总结失败(≤30 字)
- root_cause: 具体根因(哪个命令/依赖/文件导致),不确定时给出最可能原因并注明
- location: 失败位置(文件:行 / 命令 / job step)
- suggestion: 具体可执行的修复步骤(1-3 步)
- fix: 可选,给出修复代码片段(yaml/命令/代码),没有则留空字符串
输出严格 JSON,不要多余文本:
{"summary": "...", "root_cause": "...", "location": "...", "suggestion": "...", "fix": ""}
"""


def build_diagnose_messages(workflow_name: str, run_id: int, log_excerpt: str) -> list[dict[str, str]]:
    """组装诊断 messages。"""
    user = (
        f"## 失败的 workflow\n{workflow_name} (run #{run_id})\n\n"
        f"## 失败日志片段(截断)\n```\n{log_excerpt}\n```"
    )
    return [
        {"role": "system", "content": DIAGNOSE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def format_diagnosis_comment(
    diag: dict[str, str],
    *,
    workflow_name: str,
    run_id: int,
    run_url: str = "",
    model: str = "",
    tokens: int = 0,
) -> str:
    """把诊断结果渲染成 PR 评论 Markdown。"""
    lines = [
        f"## 🤖 CI 失败诊断 · {workflow_name} #{run_id}",
        "",
        f"**结论**: {diag.get('summary', '') or '-'}",
    ]
    if diag.get("location"):
        lines.append(f"**失败位置**: {diag['location']}")
    if diag.get("root_cause"):
        lines.append(f"**根因**: {diag['root_cause']}")
    if diag.get("suggestion"):
        lines.append(f"**修复建议**: {diag['suggestion']}")
    fix = diag.get("fix", "")
    if fix:
        lines += ["", "**修复示意**:", "```", fix, "```"]
    lines.append("")
    if run_url:
        lines.append(f"[查看失败日志]({run_url})")
    stats = [f"模型: {model or '-'}", f"token: {tokens}"]
    lines.append("")
    lines.append("_自动生成 · " + " · ".join(stats) + "_")
    return "\n".join(lines)
