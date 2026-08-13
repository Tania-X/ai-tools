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

import json
import re

# severity 分组标题: "### 🔴 Error (2)"
_SEVERITY_HEADER = re.compile(r"^###\s+\S+\s+(Error|Warn|Info)\s+\((\d+)\)", re.MULTILINE)
# 机器可读锚点(pr-review format_comment 输出的 HTML 注释, 契约化的可靠解析入口)
_META_BLOCK = re.compile(r"<!--AI-REVIEW-META\s*\n(.*?)\n-->", re.DOTALL)
# 无问题标记
NO_ISSUES_MARK = "未发现达到审查门槛的问题"


def parse_comment_issues(body: str) -> dict[str, int | bool | list | None]:
    """从评论 Markdown 提取各 severity 的问题数量与类别。

    优先解析机器锚点(AI-REVIEW-META, 含 category); 无锚点(旧评论)时
    回退到正文正则计数(此时无法拿到类别, categories=None)。

    返回: {"error": n, "warn": n, "info": n, "total": n, "no_issues": bool,
           "categories": [str] | None}
    """
    result: dict[str, int | bool | list | None] = {
        "error": 0, "warn": 0, "info": 0, "total": 0, "no_issues": False, "categories": None,
    }
    if not body:
        return result

    # 优先: 机器锚点(契约化解析, 含类别)
    m = _META_BLOCK.search(body)
    if m:
        try:
            meta = json.loads(m.group(1))
            issues = meta.get("issues") or []
            cats: list[str] = []
            for it in issues:
                sev = str(it.get("severity", "info")).lower()
                result[sev] = int(result[sev]) + 1  # type: ignore[operator]
                result["total"] = int(result["total"]) + 1  # type: ignore[operator]
                cat = it.get("category")
                if cat:
                    cats.append(str(cat))
            result["categories"] = cats
            result["no_issues"] = len(issues) == 0
            return result
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass  # 锚点损坏, 回退到正文正则

    # 回退: 正文正则计数(旧评论, 无类别信息)
    if NO_ISSUES_MARK in body:
        result["no_issues"] = True
    for sev, count in _SEVERITY_HEADER.findall(body):
        key = sev.lower()
        result[key] = int(result[key]) + int(count)  # type: ignore[operator]
        result["total"] = int(result["total"]) + int(count)  # type: ignore[operator]
    return result


def find_ai_review_comment(comments: list[dict]) -> str:
    """从 issue 评论列表里找 AI review 的整体评论 body(无则返回空串)。"""
    for c in comments:
        body = c.get("body", "")
        if "🤖 AI 代码审查" in body:
            return body
    return ""
