"""main.py 门禁逻辑单测(不触发真实网络)。"""

import pytest

from pr_review.config import ReviewConfig
from pr_review.main import _check_summary, _check_title, _has_blocking_issues
from pr_review.review import ReviewIssue, ReviewResult


def _result(*issues: ReviewIssue) -> ReviewResult:
    r = ReviewResult(model="deepseek-chat")
    r.issues = list(issues)
    return r


def _issue(severity: str, needs_review: bool = False) -> ReviewIssue:
    return ReviewIssue(
        file="a.py", line=1, severity=severity,
        title="t", detail="d", suggestion="s",
        needs_review=needs_review,
    )


# ---------------------------------------------------------------- 门禁判定
def test_block_on_error_by_default():
    cfg = ReviewConfig(fail_on_severity="error")
    assert _has_blocking_issues(cfg, _result(_issue("error")))
    # 只有 warn 不拦
    assert not _has_blocking_issues(cfg, _result(_issue("warn")))
    assert not _has_blocking_issues(cfg, _result(_issue("info")))


def test_needs_review_never_blocks():
    """AI 不确定(needs_review)的问题不计入门禁,避免误报阻塞合并。"""
    cfg = ReviewConfig(fail_on_severity="error")
    assert not _has_blocking_issues(cfg, _result(_issue("error", needs_review=True)))
    assert not _has_blocking_issues(cfg, _result(_issue("warn", needs_review=True)))
    # 混合: 确定的 error 仍然拦
    assert _has_blocking_issues(
        cfg, _result(_issue("error", needs_review=True), _issue("error"))
    )


def test_block_on_warn_when_configured():
    cfg = ReviewConfig(fail_on_severity="warn")
    assert _has_blocking_issues(cfg, _result(_issue("warn")))
    assert _has_blocking_issues(cfg, _result(_issue("error")))
    assert not _has_blocking_issues(cfg, _result(_issue("info")))


def test_off_never_blocks():
    cfg = ReviewConfig(fail_on_severity="off")
    assert not _has_blocking_issues(cfg, _result(_issue("error")))


def test_empty_result_never_blocks():
    cfg = ReviewConfig(fail_on_severity="error")
    assert not _has_blocking_issues(cfg, _result())


# ---------------------------------------------------------------- check-run 文本
def test_check_title_blocked():
    cfg = ReviewConfig(fail_on_severity="error")
    r = _result(_issue("error"), _issue("error"), _issue("warn"))
    title = _check_title(r, True, cfg)
    assert "2 Error" in title and "1 Warn" in title


def test_check_title_passed_with_warns():
    cfg = ReviewConfig(fail_on_severity="error")
    r = _result(_issue("warn"))
    assert "未达到门禁级别" in _check_title(r, False, cfg)


def test_check_summary_contains_stats():
    cfg = ReviewConfig(fail_on_severity="error")
    r = _result(_issue("error"), _issue("warn"), _issue("info"))
    r.total_tokens = 1000
    r.total_cost = 0.01
    summary = _check_summary(r, cfg)
    assert "1 Error" in summary and "1 Warn" in summary and "1 Info" in summary
    assert "门禁级别: error" in summary


# ---------------------------------------------------------------- severity 统计
def test_severity_counts():
    r = _result(_issue("error"), _issue("warn"), _issue("info"), _issue("warn"))
    assert r.severity_counts == {"error": 1, "warn": 2, "info": 1}


def test_config_load_fail_on_severity(tmp_path):
    f = tmp_path / ".ai-review.yaml"
    f.write_text("fail_on_severity: warn\n", encoding="utf-8")
    from pr_review.config import load_config

    assert load_config(f).fail_on_severity == "warn"
    assert load_config(None).fail_on_severity == "error"  # 默认 error
