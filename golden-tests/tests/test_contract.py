"""契约锁死测试: pr-review 的 format_comment 输出必须能被 golden parser 可靠解析。

这是 evaluation-review.md P0 项的落地——把"评论格式"从隐式耦合变成显式契约:
format_comment 输出机器锚点(AI-REVIEW-META), parser 优先解析锚点,
正文 Markdown 格式改动不再静默破坏 golden 驱动器的解析。
"""

import sys
from pathlib import Path

# 跨包测试: 需要 import pr_review(位于 pr-review/, 非当前包路径)
_REPO_ROOT = Path(__file__).resolve().parents[2]  # ai-tools 根
_PR_REVIEW = _REPO_ROOT / "pr-review"
if str(_PR_REVIEW) not in sys.path:
    sys.path.insert(0, str(_PR_REVIEW))

from golden.parser import parse_comment_issues  # noqa: E402
from pr_review.config import ReviewConfig  # noqa: E402
from pr_review.review import ReviewIssue, ReviewResult, ReviewRunner  # noqa: E402


def _format(issues: list[ReviewIssue]) -> str:
    result = ReviewResult(model="deepseek-chat")
    result.issues = issues
    runner = ReviewRunner(github=None, llm=None, config=ReviewConfig(), repo_root=None, context="")
    return runner.format_comment(result)


def test_format_comment_roundtrip_counts_and_categories():
    body = _format([
        ReviewIssue(file="src/main.go", line=21, severity=4, title="nil 解引用", detail="", suggestion="", category="bug"),
        ReviewIssue(file="src/main.go", line=12, severity=2, title="并发读写", detail="", suggestion="", category="resource"),
        ReviewIssue(file="src/main.go", line=14, severity=1, title="缺注释", detail="", suggestion="", category="convention"),
    ])
    parsed = parse_comment_issues(body)
    assert parsed["4"] == 1
    assert parsed["2"] == 1
    assert parsed["1"] == 1
    assert parsed["total"] == 3
    assert set(parsed["categories"]) == {"bug", "resource", "convention"}


def test_format_comment_no_issues_roundtrip():
    body = _format([])
    parsed = parse_comment_issues(body)
    assert parsed["total"] == 0
    assert parsed["no_issues"] is True
    assert parsed["categories"] == []
