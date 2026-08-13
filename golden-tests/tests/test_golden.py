"""golden 驱动器纯函数单测(parser / evaluate / report), 零网络依赖。"""

from golden.assert_result import evaluate
from golden.parser import find_ai_review_comment, parse_comment_issues
from golden.report import render_report


# ---------------------------------------------------------------- parser
def test_parse_comment_issues_counts_by_severity():
    body = (
        "## 🤖 AI 代码审查\n"
        "### 🔴 Error (2)\n"
        "1. **`a.go`:1** — nil 解引用\n"
        "2. **`b.go`:5** — SQL 注入\n"
        "### 🟡 Warn (1)\n"
        "1. **`c.go`:3** — 忽略错误\n"
    )
    actual = parse_comment_issues(body)
    assert actual["error"] == 2
    assert actual["warn"] == 1
    assert actual["info"] == 0
    assert actual["total"] == 3
    assert actual["no_issues"] is False


def test_parse_comment_issues_no_issues_mark():
    body = "**整体判断**:\n- 改动清晰\n\n✅ 未发现达到审查门槛的问题"
    actual = parse_comment_issues(body)
    assert actual["total"] == 0
    assert actual["no_issues"] is True


def test_parse_comment_issues_empty():
    assert parse_comment_issues("")["total"] == 0


def test_find_ai_review_comment():
    comments = [
        {"body": "普通评论"},
        {"body": "## 🤖 AI 代码审查\n### 🔴 Error (1)"},
    ]
    body = find_ai_review_comment(comments)
    assert "AI 代码审查" in body
    assert find_ai_review_comment([{"body": "无"}] ) == ""


# ---------------------------------------------------------------- evaluate
def test_evaluate_pass_when_issues_in_range():
    expected = {"expect": {"min_issues": 1, "severities": ["error"], "quality_pass": True}}
    actual = {"error": 1, "warn": 0, "info": 0, "total": 1}
    result = evaluate("case-bug", expected, actual, "failure")
    assert result["pass"] is True


def test_evaluate_fail_on_too_few_issues():
    expected = {"expect": {"min_issues": 1}}
    result = evaluate("case-bug", expected, {"total": 0, "error": 0, "warn": 0, "info": 0}, "success")
    assert result["pass"] is False
    assert any("min_issues" in f for f in result["failures"])


def test_evaluate_fail_on_wrong_severity():
    # 期望至少命中 error, 却只报出 warn(漏报 error)
    expected = {"expect": {"severities": ["error"]}}
    actual = {"error": 0, "warn": 1, "info": 0, "total": 1}
    result = evaluate("case-bug", expected, actual, "success")
    assert result["pass"] is False
    assert any("未命中" in f for f in result["failures"])


def test_evaluate_pass_when_error_hit_with_noise_warn():
    # 核心缺陷命中(error) + 额外 warn 噪音, 不应误判 fail(本次修复的核心场景)
    expected = {"expect": {"min_issues": 1, "severities": ["error"], "quality_pass": True}}
    actual = {"error": 1, "warn": 3, "info": 0, "total": 4}
    result = evaluate("case-bug", expected, actual, "failure")
    assert result["pass"] is True
    assert result["failures"] == []


def test_evaluate_forbid_severity():
    # 边界样本: 禁止报 error, 报出 error 即 fail
    expected = {"expect": {"forbid_severities": ["error"], "quality_pass": True}}
    actual_error = {"error": 1, "warn": 0, "info": 0, "total": 1}
    assert evaluate("case-bait", expected, actual_error, "failure")["pass"] is False

    # 报 warn/info 可接受
    actual_warn = {"error": 0, "warn": 1, "info": 0, "total": 1}
    assert evaluate("case-bait", expected, actual_warn, "success")["pass"] is True


def test_evaluate_fail_on_quality_gate_degraded():
    # 期望 pass 却 check neutral(质量门降级)
    expected = {"expect": {"quality_pass": True, "max_issues": 0}}
    actual = {"error": 0, "warn": 0, "info": 0, "total": 0}
    result = evaluate("case-clean", expected, actual, "neutral")
    assert result["pass"] is False
    assert any("质量门" in f for f in result["failures"])


def test_evaluate_max_issues_zero():
    # 负样本: 期望 0 issues, 却报出 1 个
    expected = {"expect": {"max_issues": 0}}
    actual = {"error": 1, "warn": 0, "info": 0, "total": 1}
    result = evaluate("case-clean", expected, actual, "failure")
    assert result["pass"] is False


# ---------------------------------------------------------------- report
def test_render_report_summary():
    results = [
        {"case": "case-bug", "pass": True, "status": None, "actual": {"total": 1}, "check_conclusion": "failure"},
        {"case": "case-clean", "pass": False, "status": None, "actual": {"total": 1}, "failures": ["误报"], "expected": {"max_issues": 0}},
        {"case": "case-docs", "pass": False, "status": "skip", "reason": "超时"},
    ]
    report = render_report(results, 0)
    assert "通过: 1 / 3" in report
    assert "case-bug" in report and "case-clean" in report
    assert "误报" in report  # 失败详情
