"""从 AI review 评论 Markdown 解析审查结果(纯函数, 无网络依赖)。

评论格式(pr-review 的 format_comment):
    ### 🔴 Error (2)
    1. **`file:line`** — title
    ### 🟡 Warn (1)
    1. **`file:line`** — title
    (无问题时)
    ✅ 未发现达到审查门槛的问题
"""

from __future__ import annotations

import re

# severity 分组标题: "### 🔴 Error (2)"
_SEVERITY_HEADER = re.compile(r"^###\s+\S+\s+(Error|Warn|Info)\s+\((\d+)\)", re.MULTILINE)
# 无问题标记
NO_ISSUES_MARK = "未发现达到审查门槛的问题"


def parse_comment_issues(body: str) -> dict[str, int | bool]:
    """从评论 Markdown 提取各 severity 的问题数量。

    返回: {"error": n, "warn": n, "info": n, "total": n, "no_issues": bool}
    """
    result: dict[str, int | bool] = {
        "error": 0, "warn": 0, "info": 0, "total": 0, "no_issues": False,
    }
    if not body:
        return result
    if NO_ISSUES_MARK in body:
        result["no_issues"] = True
    for sev, count in _SEVERITY_HEADER.findall(body):
        key = sev.lower()
        result[key] = int(result[key]) + int(count)
        result["total"] = int(result["total"]) + int(count)
    return result


def find_ai_review_comment(comments: list[dict]) -> str:
    """从 issue 评论列表里找 AI review 的整体评论 body(无则返回空串)。"""
    for c in comments:
        body = c.get("body", "")
        if "🤖 AI 代码审查" in body:
            return body
    return ""
