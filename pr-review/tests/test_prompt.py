"""prompt 组装 / JSON 解析单测。"""

import pytest

from pr_review.config import DEFAULT_CONFIG
from pr_review.diff import parse_diff
from pr_review.github import PRInfo
from pr_review.prompt import build_messages, parse_review_json

SAMPLE_DIFF = """diff --git a/src/hello.py b/src/hello.py
--- a/src/hello.py
+++ b/src/hello.py
@@ -1,5 +1,6 @@
 def greet(name):
-    return "Hello, " + name
+    return f"Hello, {name}"
"""

PR = PRInfo(
    number=42,
    title="feat: 支持 f-string",
    body="改用 f-string",
    head_sha="abc123",
    head_ref="feat/fstring",
    base_ref="main",
)


def test_build_messages_structure():
    fd = parse_diff(SAMPLE_DIFF)[0]
    messages = build_messages(PR, [fd], DEFAULT_CONFIG)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "资深代码审查专家" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    user = messages[1]["content"]
    assert "PR #42" in user
    assert "feat: 支持 f-string" in user
    assert "src/hello.py" in user
    assert "Hello" in user  # diff 内容进入 prompt


def test_system_prompt_v2_discipline():
    """v2 判断纪律: 不假设接口参数/状态管理, 需证据, 显式类型一致性检查。"""
    system = build_messages(PR, [], DEFAULT_CONFIG)[0]["content"]
    assert "结合仓库上下文中的后端接口定义" in system  # 契约判断
    assert "只有看到证据才下结论" in system            # 状态管理证据
    assert "needs_review" in system                    # 不确定标记
    assert "同名字段的类型是否一致" in system           # 显式类型一致性
    assert "evidence" in system                         # 判断依据


def test_build_messages_batch_note():
    fd = parse_diff(SAMPLE_DIFF)[0]
    messages = build_messages(PR, [fd], DEFAULT_CONFIG, batch_no=1, batch_total=2)
    assert "第 1/2 批" in messages[1]["content"]


def test_build_messages_contains_focus():
    fd = parse_diff(SAMPLE_DIFF)[0]
    messages = build_messages(PR, [fd], DEFAULT_CONFIG)
    assert "审查重点" in messages[1]["content"]
    assert "bug 与逻辑错误" in messages[1]["content"]


def test_parse_review_json_plain():
    raw = '{"summary": "ok", "issues": [{"file": "a.py", "line": 1, "severity": "warn", "title": "t", "detail": "d", "suggestion": "s"}]}'
    data = parse_review_json(raw)
    assert data["summary"] == "ok"
    assert data["issues"][0]["file"] == "a.py"


def test_parse_review_json_with_fence():
    raw = '```json\n{"summary": "ok", "issues": []}\n```'
    data = parse_review_json(raw)
    assert data["summary"] == "ok"


def test_parse_review_json_with_trailing_text():
    raw = '{"summary": "ok", "issues": []}\n以上是审查结果。'
    data = parse_review_json(raw)
    assert data["summary"] == "ok"


def test_parse_review_json_invalid():
    with pytest.raises(ValueError):
        parse_review_json("no json here")


def test_summary_requires_positive_feedback_when_no_issues():
    """prompt 要求 summary 无论是否有问题都输出(无问题时给正面评价)。"""
    system = build_messages(PR, [], DEFAULT_CONFIG)[0]["content"]
    assert "正面评价" in system
    assert "无论是否发现问题都必须输出" in system
