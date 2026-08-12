"""质量门(P1b)单测:judge 解析/结构校验/重写循环,零真实调用。"""

from unittest.mock import MagicMock, patch

from gateway import ChatResponse

from pr_review.config import QualityConfig, ReviewConfig
from pr_review.quality import (
    Judge,
    build_judge_messages,
    structural_signals,
)
from pr_review.review import ReviewIssue, ReviewResult, ReviewRunner


def _issue(file="a.py", line=1, severity="warn", title="t"):
    return ReviewIssue(
        file=file, line=line, severity=severity,
        title=title, detail="d", suggestion="s",
    )


# ---------------------------------------------------------------- judge 解析
def test_judge_parse_pass():
    result = Judge._parse('{"score": 85, "verdict": "pass", "reasons": []}')
    assert result.score == 85
    assert result.verdict == "pass"


def test_judge_parse_rewrite_with_reasons():
    result = Judge._parse(
        '{"score": 55, "verdict": "rewrite", "reasons": ["第3条是误报", "缺少覆盖度"]}'
    )
    assert result.verdict == "rewrite"
    assert "误报" in result.reasons[0]


def test_judge_parse_invalid_falls_back():
    result = Judge._parse("not json at all")
    assert result.score == 0
    assert result.verdict == "rewrite"
    assert result.reasons


# ---------------------------------------------------------------- 结构校验层
def test_structural_signals_catches_hallucination():
    issues = [
        _issue(file="a.py", line=1),
        _issue(file="a.py", line=999),          # 不在新增行
        _issue(file="b.py", line=0),            # 行号缺失
        ReviewIssue(file="c.py", line=2, severity="fatal", title="非法级别", detail="", suggestion=""),
    ]
    signals = structural_signals(issues, {"a.py": {1, 2}})
    assert any("999" in s for s in signals)        # 幻觉
    assert any("行号缺失" in s for s in signals)
    assert any("severity 非法" in s for s in signals)
    assert not any("a.py:1" in s for s in signals)  # 正常 issue 无信号


def test_structural_signals_clean():
    issues = [_issue(file="a.py", line=1)]
    assert structural_signals(issues, {"a.py": {1}}) == []


# ---------------------------------------------------------------- judge messages
def test_build_judge_messages_structure():
    result = ReviewResult()
    result.issues = [_issue(file="a.py", line=1)]
    result.quality_reasons = ["上轮扣分点"]
    cfg = QualityConfig(pass_score=70)
    messages = build_judge_messages(result, "diff-text", ["信号"], cfg)
    assert messages[0]["role"] == "system"
    assert "70" in messages[0]["content"]  # pass_score 注入
    user = messages[1]["content"]
    assert "a.py" in user and "diff-text" in user
    assert "信号" in user and "上轮扣分点" in user


def test_judge_passes_custom_model():
    llm = MagicMock()
    llm.chat.return_value = ChatResponse(
        content='{"score": 90, "verdict": "pass", "reasons": []}',
        model="m", provider="p", usage={},
    )
    judge = Judge(llm=llm, config=QualityConfig(judge_model="deepseek-r1"))
    result = ReviewResult()
    result.issues = [_issue()]
    jr = judge.evaluate(result, "diff")
    assert jr.verdict == "pass"
    assert llm.chat.call_args.kwargs["model"] == "deepseek-r1"  # 独立模型覆盖


# ---------------------------------------------------------------- 重写循环
def _quality_runner(llm_side_effect):
    """构造带指定审查 LLM 的 runner(质量循环 mock judge)。"""
    llm = MagicMock()
    llm.config.get.return_value = MagicMock(model="deepseek-chat")
    llm.chat.side_effect = llm_side_effect
    github = MagicMock()
    github.get_pr_info.return_value = MagicMock(
        number=1, title="t", head_ref="f", base_ref="main"
    )
    github.get_pr_files.return_value = [{
        "filename": "src/a.py", "status": "modified",
        "patch": "@@ -1,1 +1,2 @@\n+def foo():\n+    pass",
    }]
    return ReviewRunner(github=github, llm=llm, config=ReviewConfig()), github, llm


def _fake_judge(result, diff_text):
    """mock Judge.evaluate: 返回一个可配置的 JudgeResult。"""
    from pr_review.quality import JudgeResult

    return JudgeResult(score=70, verdict="pass", reasons=[])


def test_quality_loop_pass_no_rewrite():
    runner, _, llm = _quality_runner(
        [ChatResponse(content='{"summary": "s", "issues": []}', model="m", provider="p", usage={})]
    )
    with patch("pr_review.quality.Judge.evaluate", return_value=_fake_judge(None, "")):
        result = runner.run()
    assert result.quality_verdict == "pass"
    assert result.rewrites == 0
    assert llm.chat.call_count == 1  # 只审了一次,未重写


def test_quality_loop_rewrite_then_pass():
    responses = [
        ChatResponse(content='{"summary": "s1", "issues": [{"file": "src/a.py", "line": 1, "severity": "warn", "title": "A", "detail": "", "suggestion": ""}]}', model="m", provider="p", usage={}),
        ChatResponse(content='{"summary": "s2", "issues": [{"file": "src/a.py", "line": 1, "severity": "warn", "title": "A", "detail": "", "suggestion": ""}]}', model="m", provider="p", usage={}),
    ]
    runner, _, llm = _quality_runner(responses)

    from pr_review.quality import JudgeResult

    judge_results = iter(
        [JudgeResult(score=50, verdict="rewrite", reasons=["误报需修正"]),
         JudgeResult(score=85, verdict="pass", reasons=[])]
    )
    with patch("pr_review.quality.Judge.evaluate", side_effect=lambda r, d: next(judge_results)):
        result = runner.run()
    assert result.quality_verdict == "pass"
    assert result.rewrites == 1
    assert llm.chat.call_count == 2  # 原审 + 1 次重写


def test_quality_loop_degraded_after_max_rewrites():
    responses = [
        ChatResponse(content='{"summary": "s", "issues": []}', model="m", provider="p", usage={})
        for _ in range(4)  # 原审 + 3 次重写
    ]
    runner, _, llm = _quality_runner(responses)

    from pr_review.quality import JudgeResult

    judge_results = iter(
        [JudgeResult(score=30, verdict="rewrite", reasons=["质量差"]) for _ in range(4)]
    )
    with patch("pr_review.quality.Judge.evaluate", side_effect=lambda r, d: next(judge_results)):
        result = runner.run()
    assert result.quality_verdict == "degraded"
    assert result.rewrites == 3  # 达到 max_rewrites 硬上限
    assert llm.chat.call_count == 4


def test_quality_loop_rewrite_replaces_issues():
    """重写轮 issues 不叠加,以最后一轮为准。"""
    responses = [
        ChatResponse(
            content='{"summary": "s", "issues": [{"file": "src/a.py", "line": 1, "severity": "warn", "title": "旧问题", "detail": "", "suggestion": ""}]}',
            model="m", provider="p", usage={},
        ),
        ChatResponse(content='{"summary": "s", "issues": []}', model="m", provider="p", usage={}),
    ]
    runner, _, _ = _quality_runner(responses)

    from pr_review.quality import JudgeResult

    judge_results = iter(
        [JudgeResult(score=40, verdict="rewrite", reasons=["有误报"]),
         JudgeResult(score=80, verdict="pass", reasons=[])]
    )
    with patch("pr_review.quality.Judge.evaluate", side_effect=lambda r, d: next(judge_results)):
        result = runner.run()
    assert result.quality_verdict == "pass"
    assert len(result.issues) == 0  # 重写后空 issues 覆盖了旧 issues


# ---------------------------------------------------------------- 降级评论
def test_format_degraded_comment_includes_summary():
    runner, _, _ = _quality_runner([])
    result = ReviewResult(model="deepseek-chat", review_no=3)
    result.issues = [_issue(file="a.py", line=1, severity="error", title="真问题")]
    result.quality_score = 45.0
    result.quality_reasons = ["误报多"]
    comment = runner.format_degraded_comment(result)
    assert "质量评估未达标" in comment
    assert "45/100" in comment
    assert "误报多" in comment
    assert "真问题" in comment  # 附 issues 摘要
    assert "不阻塞" not in comment  # neutral 提示在 check-run 标题,不在评论


# ---------------------------------------------------------------- feedback 注入
def test_feedback_injected_into_prompt():
    from pr_review.diff import parse_diff
    from pr_review.prompt import build_messages

    fd = parse_diff("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,2 @@\n+def foo():")[0]
    pr = MagicMock(number=1, title="t", head_ref="f", base_ref="main")
    msgs = build_messages(pr, [fd], ReviewConfig(), feedback=["第3条是误报"])
    user = msgs[1]["content"]
    assert "上一轮质量反馈" in user
    assert "误报" in user
