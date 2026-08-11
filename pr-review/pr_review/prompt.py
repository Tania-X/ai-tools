"""Prompt 组装:把 PR 上下文 + 文件 diff 变成 LLM 输入。

输出约束:强制 LLM 返回纯 JSON(文件+行号+级别+问题+建议),便于后续解析与评论。
"""

from __future__ import annotations

import json
from typing import Any

from .config import ReviewConfig
from .diff import FileDiff
from .github import PRInfo

SYSTEM_PROMPT = """你是一位资深代码审查专家,正在审查一个 Pull Request。
你只依据提供的 diff 给出**真实、具体、可执行**的审查意见,遵循以下规则:

1. 只审查 diff 中改动过的行,不要审查未改动的既有代码。
2. 不要为了凑数而评论:没有真问题就输出空 issues 数组。
3. 每条意见必须包含文件、行号(新文件行号)、严重级别、标题、详细说明、修改建议。
4. 行号必须与 diff 中标注的新文件行号一致,无法确定行号时用 0。
5. 严重级别只能是 error / warn / info 之一:
   - error: 明确会导致 bug、安全漏洞、数据丢失
   - warn: 潜在问题、明显隐患、违反项目约定
   - info: 建议性改进(可读性、可维护性)
6. 只输出 JSON,不要输出任何解释或 markdown 围栏。

输出 JSON 结构(严格遵循):
{
  "summary": "对这个 PR 的整体判断,1-2 句话",
  "issues": [
    {
      "file": "src/foo.py",
      "line": 42,
      "severity": "warn",
      "title": "简短的问题标题",
      "detail": "问题描述,说明为什么是问题",
      "suggestion": "具体的修改建议"
    }
  ]
}
"""


def build_messages(
    pr_info: PRInfo,
    files: list[FileDiff],
    config: ReviewConfig,
    batch_no: int | None = None,
    batch_total: int | None = None,
) -> list[dict[str, str]]:
    """组装一轮审查的 messages([system, user])。

    多个文件按批次传入;批次序号用于提示 LLM 这是整体的一部分。
    """
    focus_text = "\n".join(f"- {f}" for f in config.review_focus)
    batch_note = (
        f"\n注意:本次是第 {batch_no}/{batch_total} 批,只给出这批文件的问题。"
        if batch_no and batch_total
        else ""
    )

    parts: list[str] = [
        f"# PR #{pr_info.number}: {pr_info.title}",
        f"分支: {pr_info.head_ref} -> {pr_info.base_ref}",
        f"变更文件数: {len(files)}",
        "",
        "## 审查重点(按重要度排序)",
        focus_text,
        "",
        "## 变更内容(diff)",
        "",
    ]
    for fd in files:
        parts.append(f"### 文件: {fd.path} (状态: {fd.status})")
        parts.extend(fd.to_display_lines())
        parts.append("")

    user_content = "\n".join(parts).strip() + batch_note
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_review_json(content: str) -> dict[str, Any]:
    """容错解析 LLM 输出的 JSON。

    LLM 偶尔会加 ```json 围栏或前后多余文本,这里做轻量清理。
    """
    text = content.strip()
    # 去掉 markdown 围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # 截取第一个 { 到最后一个 }(容错额外说明文本)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 输出中未找到 JSON 对象: {content[:200]!r}")
    return json.loads(text[start : end + 1])
