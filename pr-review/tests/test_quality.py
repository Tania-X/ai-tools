"""质量门(P1b)单测:judge 解析/结构校验/重写循环,零真实调用。"""

from unittest.mock import MagicMock, patch

from gateway import ChatResponse

from pr_review.config import QualityConfig, ReviewConfig
from pr_review.quality import (
    ACTION_DELETE,
    ACTION_DOWNGRADE,
    ACTION_KEEP,
    IssueVerdict,
    Judge,
    build_judge_messages,
    per_issue_verify,
    sentinel_triggered,
    structural_signals,
)
from pr_review.review import ReviewIssue, ReviewResult, ReviewRunner


def _issue(file="a.py", line=1, severity=2, title="t"):
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
        ReviewIssue(file="c.py", line=2, severity=99, title="越界级别", detail="", suggestion=""),
    ]
    signals = structural_signals(issues, {"a.py": {1, 2}})
    assert any("999" in s for s in signals)        # 幻觉
    assert any("行号缺失" in s for s in signals)
    assert any("severity 越界" in s for s in signals)
    assert not any("a.py:1" in s for s in signals)  # 正常 issue 无信号


def test_structural_signals_clean():
    issues = [_issue(file="a.py", line=1)]
    assert structural_signals(issues, {"a.py": {1}}) == []


def test_structural_signals_catches_severity_overgrade():
    """2026-08-19 P1: 证据是假设性故障但级别 ≥4(会拦合并) → 疑似高判信号。"""
    issues = [
        _issue(file="a.py", line=1),  # 正常
        ReviewIssue(
            file="b.py", line=3, severity=4, title="策略清理失败",
            detail="若 RemoveFilteredPolicy 失败, 权限可能残留",
            suggestion="", evidence="第 350 行, 失败时无回滚",
        ),
        ReviewIssue(  # 高严重度但证据是确定性的 → 不应出信号
            file="c.py", line=5, severity=5, title="nil 解引用",
            detail="user 为 nil, 直接访问 .Name 必然 panic",
            suggestion="", evidence="第 21 行必有 nil",
        ),
    ]
    signals = structural_signals(issues, {"a.py": {1}, "b.py": {3}, "c.py": {5}})
    hits = [s for s in signals if "疑似严重度高判" in s]
    assert len(hits) == 1, f"应只有 b.py 高判, 实际 {signals}"
    assert "b.py:3" in hits[0]
    # 确定性证据的 5 级不误报
    assert not any("c.py" in s for s in signals)


def test_judge_rubric_has_severity_match():
    """judge rubric 含严重度与证据匹配维度(2026-08-19 P1)。"""
    from pr_review.quality import JUDGE_SYSTEM_PROMPT

    assert "严重度与证据匹配" in JUDGE_SYSTEM_PROMPT
    assert "高判" in JUDGE_SYSTEM_PROMPT


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


def test_judge_passes_custom_provider():
    """模型路由: judge 独立 provider(审查用主 provider, judge 用便宜 provider)。"""
    llm = MagicMock()
    llm.chat.return_value = ChatResponse(
        content='{"score": 90, "verdict": "pass", "reasons": []}',
        model="m", provider="p", usage={},
    )
    judge = Judge(
        llm=llm,
        config=QualityConfig(judge_model="deepseek-v4-flash", judge_provider="kimi"),
    )
    result = ReviewResult()
    result.issues = [_issue()]
    jr = judge.evaluate(result, "diff")
    assert jr.verdict == "pass"
    assert llm.chat.call_args.kwargs["model"] == "deepseek-v4-flash"
    assert llm.chat.call_args.kwargs["provider"] == "kimi"  # 独立 provider 路由


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
    # 非空 issues(有代码问题)→ 走完整 3 次重写路径
    issues_json = (
        '{"file": "src/a.py", "line": 1, "severity": "warn", "title": "A", "detail": "", "suggestion": ""}'
    )
    responses = [
        ChatResponse(
            content='{"summary": "s", "issues": [' + issues_json + "]}",
            model="m", provider="p", usage={},
        )
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
    result.issues = [_issue(file="a.py", line=1, severity=4, title="真问题")]
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


# ---------------------------------------------------------------- 空 issues 短路(线上 bug 修复)
def test_quality_loop_skips_judge_when_no_code_changes():
    """纯文档/配置 PR(无代码变更)且 issues 空 → 直接 pass, judge 不调用(零浪费)。"""
    runner, github, llm = _quality_runner(
        [ChatResponse(content='{"summary": "无问题", "issues": []}', model="m", provider="p", usage={})]
    )
    # 覆盖 PR 文件为纯文档(AGENTS.md)
    github.get_pr_files.return_value = [{
        "filename": "AGENTS.md", "status": "modified",
        "patch": "@@ -1,1 +1,2 @@\n+new rule",
    }]
    with patch("pr_review.quality.Judge.evaluate") as mocked_judge:
        result = runner.run()
    assert result.quality_verdict == "pass"
    assert result.rewrites == 0
    mocked_judge.assert_not_called()  # 关键: judge 完全不调用
    assert llm.chat.call_count == 1   # 只审一次


def test_quality_loop_empty_issues_with_code_checks_once():
    """有代码变更但 issues 空 → 只做一次漏报检查(重写上限 1), 不无限循环。"""
    from pr_review.quality import JudgeResult

    responses = [
        ChatResponse(content='{"summary": "s", "issues": []}', model="m", provider="p", usage={}),
        ChatResponse(content='{"summary": "s", "issues": []}', model="m", provider="p", usage={}),
    ]
    runner, _, llm = _quality_runner(responses)
    from pr_review.diff import parse_diff

    runner._last_candidates = [
        parse_diff("diff --git a/src/main.go b/src/main.go\n--- a/src/main.go\n+++ b/src/main.go\n@@ -1,1 +1,2 @@\n+func main() {}")[0]
    ]
    judge_results = iter(
        [JudgeResult(score=30, verdict="rewrite", reasons=["漏报"]),
         JudgeResult(score=30, verdict="rewrite", reasons=["仍漏报"])]
    )
    with patch("pr_review.quality.Judge.evaluate", side_effect=lambda r, d: next(judge_results)):
        result = runner.run()
    assert result.quality_verdict == "degraded"
    assert result.rewrites == 1  # 空 issues 时重写上限 1, 不是 3
    assert llm.chat.call_count == 2


def test_has_code_changes_detects_extensions():
    runner, _, _ = _quality_runner([])
    from pr_review.diff import parse_diff

    runner._last_candidates = [
        parse_diff("diff --git a/AGENTS.md b/AGENTS.md\n--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1 @@\n+x")[0]
    ]
    assert runner._has_code_changes() is False
    runner._last_candidates = [
        parse_diff("diff --git a/src/a.go b/src/a.go\n--- a/src/a.go\n+++ b/src/a.go\n@@ -1 +1 @@\n+x")[0]
    ]
    assert runner._has_code_changes() is True


def test_judge_prompt_has_empty_issues_rule():
    """judge prompt 明确'文档变更空 issues = 正确审查'规则。"""
    from pr_review.quality import build_judge_messages

    result = ReviewResult()
    cfg = QualityConfig(pass_score=70)
    system = build_judge_messages(result, "diff", [], cfg)[0]["content"]
    assert "issues 为空数组" in system
    assert "正确审查" in system


# ---------------------------------------------------------------- 逐条验证层(2026-08-20 改造)
def test_per_issue_verify_keeps_good_issue():
    """正常 issue(行号在 diff 内、级别合法、证据确定性) → keep。"""
    from pr_review.quality import ACTION_KEEP, per_issue_verify

    issues = [_issue(file="a.py", line=1, severity=2)]
    verdicts = per_issue_verify(issues, {"a.py": {1}})
    assert len(verdicts) == 1
    assert verdicts[0].action == ACTION_KEEP


def test_per_issue_verify_deletes_hallucination_and_missing_line():
    """行号不在 diff 新增行(幻觉) / 行号缺失 → delete。"""
    from pr_review.quality import ACTION_DELETE, per_issue_verify

    issues = [
        _issue(file="a.py", line=999),      # 幻觉
        _issue(file="b.py", line=0),        # 行号缺失
    ]
    verdicts = per_issue_verify(issues, {"a.py": {1}})
    actions = [v.action for v in verdicts]
    assert actions == [ACTION_DELETE, ACTION_DELETE]


def test_per_issue_verify_downgrades_hypothetical_high_severity():
    """假设性证据 + 级别 ≥4 → downgrade 到 3(不拦合并)。"""
    from pr_review.quality import ACTION_DOWNGRADE, per_issue_verify

    issues = [
        ReviewIssue(
            file="b.py", line=3, severity=4, title="策略清理失败",
            detail="若 RemoveFilteredPolicy 失败, 权限可能残留",
            suggestion="", evidence="第 350 行, 失败时无回滚",
        ),
    ]
    verdicts = per_issue_verify(issues, {"b.py": {3}})
    assert verdicts[0].action == ACTION_DOWNGRADE
    assert verdicts[0].new_severity == 3


def test_per_issue_verify_does_not_downgrade_deterministic():
    """确定性证据的 5 级(必然 panic) → keep, 不降级。"""
    from pr_review.quality import ACTION_KEEP, per_issue_verify

    issues = [
        ReviewIssue(
            file="c.py", line=5, severity=5, title="nil 解引用",
            detail="user 为 nil, 直接访问 .Name 必然 panic",
            suggestion="", evidence="第 21 行必有 nil",
        ),
    ]
    verdicts = per_issue_verify(issues, {"c.py": {5}})
    assert verdicts[0].action == ACTION_KEEP


def test_per_issue_verify_fixes_out_of_range_severity():
    """severity 越界 → fix 钳制到合法范围。"""
    from pr_review.quality import ACTION_FIX, per_issue_verify

    issues = [
        ReviewIssue(file="c.py", line=2, severity=99, title="越界", detail="", suggestion=""),
    ]
    verdicts = per_issue_verify(issues, {"c.py": {2}})
    assert verdicts[0].action == ACTION_FIX
    assert verdicts[0].new_severity == 5  # 钳制到 5


def test_apply_verdicts_filters_and_downgrades():
    """apply_verdicts: 删除剔除, 降级改 severity, 保留不动。"""
    from pr_review.quality import (
        ACTION_DELETE,
        ACTION_DOWNGRADE,
        ACTION_KEEP,
        IssueVerdict,
        apply_verdicts,
    )

    good = _issue(file="a.py", line=1, severity=2)
    bad = _issue(file="a.py", line=999, severity=4)
    downgrade = _issue(file="b.py", line=3, severity=4)
    verdicts = [
        IssueVerdict(issue=good, action=ACTION_KEEP),
        IssueVerdict(issue=bad, action=ACTION_DELETE, reason="幻觉"),
        IssueVerdict(issue=downgrade, action=ACTION_DOWNGRADE, new_severity=3),
    ]
    kept = apply_verdicts(verdicts)
    assert len(kept) == 2  # bad 被删除
    assert kept[0] is good  # 保留的不动
    assert kept[1].severity == 3  # 降级生效


def test_sentinel_triggered_threshold():
    """删除+降级比例 > 30% → 哨兵触发; 低于 → 不触发。"""
    from pr_review.quality import (
        ACTION_DELETE,
        ACTION_KEEP,
        IssueVerdict,
        sentinel_triggered,
    )

    # 5 条里 2 条被处理 = 40% > 30% → 触发
    verdicts = [
        IssueVerdict(issue=_issue(file=f"a{i}.py", line=1), action=ACTION_KEEP)
        for i in range(3)
    ] + [
        IssueVerdict(issue=_issue(file=f"b{i}.py", line=999), action=ACTION_DELETE)
        for i in range(2)
    ]
    assert sentinel_triggered(verdicts) is True

    # 5 条里 1 条被处理 = 20% < 30% → 不触发
    verdicts = [
        IssueVerdict(issue=_issue(file=f"a{i}.py", line=1), action=ACTION_KEEP)
        for i in range(4)
    ] + [IssueVerdict(issue=_issue(file="b.py", line=999), action=ACTION_DELETE)]
    assert sentinel_triggered(verdicts) is False

    # 空列表不触发
    assert sentinel_triggered([]) is False


# ---------------------------------------------------------------- 严重度高判降级(2026-08-24)
def _hi(file="a.py", line=1, sev=4, category="other", trigger="", detail="d", evidence="e"):
    return ReviewIssue(
        file=file, line=line, severity=sev, title="t", detail=detail, suggestion="s",
        category=category, evidence=evidence, trigger=trigger,
    )


def test_downgrade_convention_high_severity():
    """纯约定违反(category=convention)判 4 → 建议档 2(P0-5: 约定类不计门禁)。"""
    issue = _hi(sev=4, category="convention", detail="clearPolicy 返回 error 未处理",
                evidence="策略清理失败会被静默掩盖, 造成数据不一致")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE
    assert v.new_severity == 2
    assert "建议档" in v.reason


def test_downgrade_hypothetical_wording():
    """假设性措辞(万一/失败会)即使 category=bug 也降级。"""
    issue = _hi(sev=4, category="bug", evidence="万一请求失败会导致连接池耗尽")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 3


def test_downgrade_trigger_hypothetical():
    """LLM 自标 trigger=hypothetical + 4 级 → 降级 3。"""
    issue = _hi(sev=4, trigger="hypothetical", evidence="若 X 失败则…")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 3


def test_real_security_bug_not_downgraded():
    """真实安全 bug(SQL 注入, trigger=real, category=security)不降级。"""
    issue = _hi(sev=5, category="security", trigger="real", detail="SQL 字符串拼接注入",
                evidence="id 为用户输入直接拼接进 query")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_KEEP


def test_real_bug_not_downgraded():
    """真实 bug(nil 解引用, trigger=real)不降级。"""
    issue = _hi(sev=5, category="bug", trigger="real", detail="getName 对 nil 解引用必然 panic")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_KEEP


def test_convention_severity3_downgraded_to_advisory():
    """P0-5(2026-09-13 拍板): 约定违反即使是 3 级(必修)也降到建议档 2。

    原先的锚点"明确约定违反=3(必修不阻塞)"已废止: 约定类问题该由 ruff 与仓库约定文档
    负责, 评审报它既重复又与门禁无关。
    """
    issue = _hi(sev=3, category="convention")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 2


def test_convention_already_advisory_stays_untouched():
    """反向守卫: 已经在建议档(≤2)的约定问题不再重复降级(不产生无意义的 verdict)。"""
    for sev in (1, 2):
        issue = _hi(sev=sev, category="convention")
        assert per_issue_verify([issue], {"a.py": {1}})[0].action == ACTION_KEEP


def test_convention_tier_not_affected_by_other_signals():
    """反向守卫: 约定档位与"高判信号"无关——带假设性措辞、自认推演都仍是 2。"""
    variants = [
        _hi(sev=4, category="convention", evidence="万一失败会导致权限残留"),
        _hi(sev=4, category="convention", evidence="约定要求处理 error", detail="d"),
        _hi(sev=5, category="convention", evidence="", detail="d"),
    ]
    variants[2].verification = "none: 属推演"
    for issue in variants:
        v = per_issue_verify([issue], {"a.py": {1}})[0]
        assert v.action == ACTION_DOWNGRADE and v.new_severity == 2


# ---------------------------------------------------------------- P0-1 可验证性字段
def _gate(sev=4, verification="", evidence="e", **kw):
    """门禁级(≥4)issue 构造器(P0-1 用例)。"""
    return ReviewIssue(
        file=kw.pop("file", "a.py"), line=kw.pop("line", 1), severity=sev,
        title="t", detail=kw.pop("detail", "d"), suggestion="s",
        evidence=evidence, verification=verification, **kw,
    )


def test_p01_speculative_verification_downgrades_gate_level():
    """P0-1: LLM 自认可验证路径属推演(none: 属推演)+ 门禁级 → 降级到 2(轻微)。

    比"无依据"更弱一档: 自认拿不出路径 = 映射表里的"无路径", 按报告 P0-1 降到轻微级。
    """
    issue = _gate(sev=4, verification="none: 属推演", evidence="看代码像是会有问题")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE
    assert v.new_severity == 2
    assert "推演" in v.reason


def test_p01_english_none_sentence_is_not_speculation():
    """P0-1 反向守卫: "None of the tests cover this" 是说明而非自认推演 → 不降级。

    只认文档约定的短形式(none: 属推演), 否则英文句子会被误判成推演, 真问题被降级。
    """
    issue = _gate(
        sev=5, verification="None of the existing tests cover this branch",
        evidence="第 21 行 user 未判空即解引用 .Name",
    )
    assert per_issue_verify([issue], {"a.py": {1}})[0].action == ACTION_KEEP


def test_p01_missing_verification_with_evidence_is_kept():
    """P0-1: verification 为空但 evidence 是事实锚点 → 不降级(避免误伤真问题)。

    这是"字段缺失"与"无依据"的分界: 只凭新字段缺失就降级, 会让不认新字段的模型
    把所有门禁级问题一起降级, 属严重信号丢失。
    """
    issue = _gate(sev=5, verification="", evidence="第 21 行 user 未判空即解引用 .Name")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_KEEP


def test_p01_gate_level_without_any_basis_downgrades():
    """P0-1: 门禁级但既无 verification 也无 evidence → 无事实支点, 降级到 3(不是 2)。

    与"自认推演"分档: 无依据仍是"未说明", 保留必修可见性; 自认推演才算轻微级。
    """
    issue = _gate(sev=4, verification="", evidence="")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE
    assert v.new_severity == 3
    assert "依据" in v.reason or "验证" in v.reason


def test_p01_level3_without_evidence_only_signals():
    """P0-1: ≥3 缺 evidence 只出信号(交 judge 参考), 不降级 3→2(白丢"必修"标记)。"""
    issue = _gate(sev=3, verification="", evidence="")
    assert per_issue_verify([issue], {"a.py": {1}})[0].action == ACTION_KEEP
    signals = structural_signals([issue], {"a.py": {1}})
    assert any("缺判断依据" in s for s in signals)


def test_p01_below_gate_level_untouched():
    """P0-1 只针对门禁级: 3 级问题没有 verification 也不动(不降级)。"""
    issue = _gate(sev=3, verification="", evidence="")
    assert per_issue_verify([issue], {"a.py": {1}})[0].action == ACTION_KEEP


def test_p01_structural_signal_flags_baseless_gate_level():
    """P0-1: 无任何事实支点的门禁级问题出信号; 有 evidence 的不出(避免噪音)。"""
    issues = [
        _gate(sev=4, verification="", evidence=""),                       # 无支点
        _gate(sev=5, verification="", evidence="第 21 行必有 nil", file="b.py"),
        _gate(sev=4, verification="none: 属推演", evidence="嗯", file="c.py"),
    ]
    signals = structural_signals(issues, {"a.py": {1}, "b.py": {1}, "c.py": {1}})
    assert any("a.py:1" in s and "无可验证路径" in s for s in signals)
    assert any("c.py:1" in s and "推演" in s for s in signals)
    assert not any("b.py" in s for s in signals)


def test_p01_review_issue_parses_verification_field():
    """P0-1: LLM 输出里的 verification 要落进 ReviewIssue(否则模型写了也没用)。"""
    issue = ReviewIssue.from_dict({
        "file": "a.py", "line": 3, "severity": "blocker", "title": "t",
        "detail": "d", "suggestion": "s", "evidence": "e",
        "verification": "POST /api/x 传 tenant_id=abc 返回 500",
    })
    assert issue.verification.startswith("POST /api/x")


def test_p01_prompt_requires_verification():
    """P0-1: prompt 规则 18 + JSON schema 必须要求 verification(模型只按 prompt 走)。"""
    from pr_review.prompt import SYSTEM_PROMPT

    assert "可验证性" in SYSTEM_PROMPT
    assert "verification" in SYSTEM_PROMPT
    assert "属推演" in SYSTEM_PROMPT


# ---------------------------------------------------------------- P0-3 判官故障留档
def test_p03_parse_failure_marks_flag_and_keeps_raw():
    """P0-3: 解析失败必须显式(parse_failed=True)+ 原文留档, 且不伪装成"0 分判负"。"""
    from pr_review.quality import Judge

    jr = Judge._parse("我觉得这个 PR 挺好的, 但有几点建议…")
    assert jr.parse_failed is True
    assert jr.raw.startswith("我觉得这个 PR")
    assert jr.verdict == "rewrite"
    assert any("judge" in r for r in jr.reasons)  # 说明是 judge 故障, 不是审查结论


def test_p03_normal_parse_has_no_flag():
    """P0-3 反向守卫: 正常解析不能被误标为故障(否则所有审查都进 judge_error 分支)。"""
    from pr_review.quality import Judge

    jr = Judge._parse('{"score": 88, "verdict": "pass", "reasons": []}')
    assert jr.parse_failed is False
    assert jr.raw == ""
    assert jr.score == 88


def test_p03_evaluate_retries_once_then_succeeds():
    """P0-3: 首次解析失败 → 严格 JSON 指令重试一次, 成功则不算故障。"""
    from pr_review.quality import Judge

    llm = MagicMock()
    llm.chat.side_effect = [
        ChatResponse(content="这是解释文字, 没有 JSON", model="m", provider="p", usage={}),
        ChatResponse(content='{"score": 77, "verdict": "pass", "reasons": []}', model="m", provider="p", usage={}),
    ]
    jr = Judge(llm=llm, config=QualityConfig(pass_score=70)).evaluate(
        ReviewResult(added_lines={}), "diff"
    )
    assert jr.parse_failed is False
    assert jr.score == 77
    assert llm.chat.call_count == 2
    # 重试请求里必须带上"只输出 JSON"的严格指令
    retry_messages = llm.chat.call_args_list[1][0][0]
    assert any("只输出一个 JSON" in m["content"] for m in retry_messages)


def test_p03_evaluate_double_failure_is_flagged_once():
    """P0-3: 两次都解析失败 → 标故障并停下(不无限重试)。"""
    from pr_review.quality import Judge

    llm = MagicMock()
    llm.chat.side_effect = [
        ChatResponse(content="nope", model="m", provider="p", usage={}),
        ChatResponse(content="still nope", model="m", provider="p", usage={}),
    ]
    jr = Judge(llm=llm, config=QualityConfig(pass_score=70)).evaluate(
        ReviewResult(added_lines={}), "diff"
    )
    assert jr.parse_failed is True
    assert llm.chat.call_count == 2  # 只重试一次


def test_p03_judge_failure_short_circuits_without_rewrite():
    """P0-3: judge 故障 → verdict=judge_error, 不触发重写(重写照样解析不出, 只烧 token)。"""
    responses = [
        ChatResponse(
            content='{"summary": "s", "issues": [{"file": "src/a.py", "line": 1, "severity": "warn", "title": "A", "detail": "", "suggestion": ""}]}',
            model="m", provider="p", usage={},
        )
        for _ in range(4)
    ]
    runner, _, llm = _quality_runner(responses)

    from pr_review.quality import JudgeResult

    def _boom(result, diff_text):
        return JudgeResult(score=0, verdict="rewrite", reasons=["judge 输出无法解析"],
                           parse_failed=True, raw="我觉得…")

    with patch("pr_review.quality.Judge.evaluate", side_effect=_boom):
        result = runner.run()

    assert result.quality_verdict == "judge_error"
    assert result.quality_parse_failed is True
    assert result.quality_score is None       # 不给 0 分(0 分会被读成"质量差")
    assert result.quality_raw_output == "我觉得…"  # 原文从 judge 结果搬进 ReviewResult(留档)
    assert result.rewrites == 0               # 没重写
    assert llm.chat.call_count == 1           # 只审了一次
    assert result.issues                    # 审查产出保留(issues 是产品, 不静默丢弃)


def test_p03_comment_marks_gate_not_effective():
    """P0-3: 评论里显式写明"质量门未生效"(工具故障), 不写成"质量未达标"(判负)。"""
    runner, _, _ = _quality_runner([])
    result = ReviewResult(model="m", review_no=2)
    result.quality_parse_failed = True
    result.quality_verdict = "judge_error"
    result.issues = [_issue(file="a.py", line=1, severity=2)]
    comment = runner.format_comment(result)
    assert "质量评分不可用" in comment
    assert "judge" in comment
    assert "质量未达标" not in comment  # 不能写成"判负"(那是另一回事)


def test_r1_3_judge_failure_explained_exactly_once():
    """R1-3 回归: judge 故障的说明在 check 文案里只能出现一次(原先 output.py 与 main.py 各写一遍)。"""
    from pr_review.output import check_run_payload

    runner, _, _ = _quality_runner([])
    result = ReviewResult(review_no=1)
    result.quality_parse_failed = True
    result.quality_verdict = "judge_error"
    result.issues = [_issue(file="a.py", line=1, severity=2)]
    title, summary = check_run_payload(result, False, runner.config, judge_failed=True)
    assert title.endswith("judge 故障")
    assert summary.count("judge") == 1, f"judge 故障说明重复: {summary}"
    assert "评分与门禁解耦" in summary

    # 反向守卫: 正常轮次不得出现 judge 故障标记
    ok_title, ok_summary = check_run_payload(ReviewResult(), False, runner.config)
    assert "judge" not in ok_title and "不可用" not in ok_summary


def test_r1_4_archives_both_judge_raw_outputs():
    """R1-4 回归: 两次都解析失败时, 首次与重试的原文都要留档(首次才是真实故障形态)。"""
    from pr_review.quality import Judge

    llm = MagicMock()
    llm.chat.side_effect = [
        ChatResponse(content="首次输出: 我觉得这个 PR 不错", model="m", provider="p", usage={}),
        ChatResponse(content="重试输出: 依然不是 JSON", model="m", provider="p", usage={}),
    ]
    jr = Judge(llm=llm, config=QualityConfig(pass_score=70)).evaluate(
        ReviewResult(added_lines={}), "diff"
    )
    assert jr.parse_failed is True
    assert "[first]" in jr.raw and "我觉得这个 PR 不错" in jr.raw
    assert "[retry]" in jr.raw and "依然不是 JSON" in jr.raw


def test_p03_comment_archives_judge_raw_output():
    """P0-3: judge 原文进评论折叠块留档(只写日志的话, 日志过期就查不出来了)。"""
    runner, _, _ = _quality_runner([])
    result = ReviewResult(model="m", review_no=2)
    result.quality_parse_failed = True
    result.quality_raw_output = "我觉得这个 PR 整体不错, 但有 3 点建议…"
    result.issues = [_issue(file="a.py", line=1, severity=2)]
    comment = runner.format_comment(result)
    assert "<details>" in comment and "judge 原始输出" in comment
    assert "我觉得这个 PR 整体不错" in comment  # 原文可查


def test_p03_no_raw_block_when_judge_healthy():
    """P0-3 反向守卫: judge 正常时不得出现折叠留档块(否则正常评论被污染)。"""
    runner, _, _ = _quality_runner([])
    result = ReviewResult(model="m", review_no=2)
    result.quality_score = 88.0
    result.quality_verdict = "pass"
    result.issues = [_issue(file="a.py", line=1, severity=2)]
    comment = runner.format_comment(result)
    assert "judge 原始输出" not in comment
    assert "质量评分不可用" not in comment


def test_p03_stats_footer_reports_unavailable_score():
    """P0-3: 统计脚注显示"不可用", 而不是 0/100(0 分是错误信息)。"""
    from pr_review.output import check_summary

    runner, _, _ = _quality_runner([])
    result = ReviewResult()
    result.quality_parse_failed = True
    summary = check_summary(result, runner.config)
    assert "不可用" in summary
    assert "0/100" not in summary
    assert "评分与门禁解耦" in summary      # R1-3: 政策说明也在同一处给出


def test_p03_judge_failure_does_not_change_issue_gate():
    """P0-3: 评分与门禁解耦 —— judge 故障不改变 issue 门禁判定。

    报告 P0-3 明确要求"不改变 issue 门禁": 否则一次 judge 抖动就能把 4 级问题的
    failure 变成放行(或反过来), 门禁含义随工具状态漂移。
    """
    from pr_review.output import has_blocking_issues

    runner, _, _ = _quality_runner([])
    result = ReviewResult()
    result.issues = [_issue(file="a.py", line=1, severity=4)]  # 达门禁级
    assert has_blocking_issues(runner.config, result) is True
    result.quality_parse_failed = True
    result.quality_verdict = "judge_error"
    assert has_blocking_issues(runner.config, result) is True  # 故障不改变结论


# ---------------------------------------------------------------- 评审 R1 回归(2026-09-13)
def test_r1_1_real_repro_path_with_trailing_unverifiable_note_is_kept():
    """R1-1 回归: 给了真实复现路径、后半句补充"无法验证" → 不得判成自认推演。

    这是评审第 1 条给的原始例子。若标记词对整条文本做子串匹配, 这条真问题会被
    降到 2(解除合并阻塞)——降级方向正好反了。
    """
    issue = _gate(
        sev=4,
        verification="复现: POST /api/x 传 tenant_id=abc 返回 500; 并发场景无法验证",
        evidence="routers/x.py:31 直接取 body['tenant_id']",
    )
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_KEEP


def test_r1_1_speculation_still_caught_in_head_window():
    """R1-1 反向守卫: 模式先行写的自认推演仍要被抓到(修复不能把功能一起修没)。"""
    for text in (
        "none: 属推演",
        "该结论属推演, 因为拿不出可复现路径",
        "无法验证: 由语言先验推断",
        "n/a",
    ):
        v = per_issue_verify([_gate(sev=4, verification=text)], {"a.py": {1}})[0]
        assert v.action == ACTION_DOWNGRADE, f"{text} 应被判为自认推演"
        assert v.new_severity == 2


def test_r1_1_none_sentence_guard_still_holds():
    """R1-1 反向守卫: 英文长句 "None of the existing tests…" 仍不算推演。"""
    issue = _gate(sev=4, verification="None of the existing tests cover this branch",
                  evidence="x.py:9 未判空")
    assert per_issue_verify([issue], {"a.py": {1}})[0].action == ACTION_KEEP


def test_r1_2_tier_is_single_source_of_truth():
    """R1-2: "是否算高判"与"降到几档"必须来自同一函数, 不允许两处各写一套顺序。

    评审第 2 条指出的真实问题不是档位错, 而是判定逻辑分散在两处、顺序不同易漂移。
    """
    from pr_review.quality import _high_judgement, _is_severity_high_judgement

    samples = [
        _gate(sev=5, verification="复现: pytest tests/test_x.py::test_y 失败", evidence="x.py:1"),
        _gate(sev=4, verification="none: 属推演", evidence="像是会有问题"),
        _gate(sev=4, verification="", evidence=""),
        _gate(sev=4, verification="", evidence="", detail="若失败则权限残留"),
        _gate(sev=3, verification="", evidence=""),
    ]
    for issue in samples:
        high = _high_judgement(issue)
        # 两个入口必须一致: 谓词 = 分档结果非空
        assert _is_severity_high_judgement(issue) is (high is not None)
        if high is not None:
            tier, signal = high
            assert tier in (2, 3) and signal
            verdict = per_issue_verify([issue], {issue.file: {issue.line}})[0]
            assert verdict.new_severity == tier, "per_issue_verify 必须用同一档位"


def test_r1_2_convention_has_its_own_rule_not_the_tier_chain():
    """R1-2 演进(P0-5 后): 约定违反不再挂在"高判分档链"上, 而是独立的建议档规则。

    这条历史值得记住: 之前 convention 是高判链的第一优先级(→3); 用户拍板"按 P0-5 来"后
    改为独立规则(→2), 且与 severity 是否 ≥4 无关——所以 `_high_judgement` 里不再有 convention。
    """
    from pr_review.quality import _high_judgement

    issue = _gate(sev=4, verification="none: 属推演", evidence="约定违反", category="convention")
    # 分档链里没有 convention 分支了(它由 4a 规则处理)
    assert _high_judgement(issue) == (2, "speculative")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 2
    assert "建议档" in v.reason      # 且是 4a 的文案, 不是推演文案


# ---------------------------------------------------------------- 评审 R2 回归(2026-09-13)
def test_r2_1_path_beats_generic_marker():
    """R2-1 回归: 给出具体路径时, 通用标记词(n/a)与补充说明都不能把它判成推演。

    评审给的原始例子: "n/a 不适用, 复现: POST /api/x …" 与 "N/A 见 tests/test_x.py::test_y"
    在修复前会命中头部窗口里的 n/a → 降到 2 → 解除合并阻塞(方向反了)。
    """
    for text in (
        "n/a 不适用, 复现: POST /api/x 传 tenant_id=abc 返回 500",
        "N/A 见 tests/test_x.py::test_y",
        "无法验证并发场景, 但可复现: POST /api/x 返回 500",
        "该分支无测试覆盖, 参考 `pytest tests/test_x.py -k y`",
    ):
        issue = _gate(sev=4, verification=text, evidence="x.py:9 直接取 body['tenant_id']")
        v = per_issue_verify([issue], {"a.py": {1}})[0]
        assert v.action == ACTION_KEEP, f"{text} 给出了路径, 不该被判成推演"


def test_r2_1_bare_escape_hatch_still_speculative():
    """R2-1 反向守卫: 整条就是逃生口(短形式)仍要判推演, 修复不能把功能修没。"""
    for text in ("none: 属推演", "n/a", "N/A 不适用"):
        v = per_issue_verify([_gate(sev=4, verification=text)], {"a.py": {1}})[0]
        assert v.action == ACTION_DOWNGRADE and v.new_severity == 2, f"{text} 仍应算推演"


def test_r2_1_tail_positioned_self_admission_without_path_is_caught():
    """R2-1: 没给路径、自认写在句子后面的, 仍要判推演。

    这正是删掉"头部窗口"的理由: 那个窗口会漏掉这类自认(它只在开头 24 字里找标记词),
    而路径优先判定已经解决了窗口当初要解决的问题。
    """
    issue = _gate(sev=4, verification="该分支未见用例, 具体行为无法验证, 只能推测")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 2


def test_r2_1_speculation_without_any_path_still_caught():
    """R2-1 反向守卫: 拿不出路径的自认(提了工具名但没有具体用例)仍要判推演。"""
    issue = _gate(sev=4, verification="无法给出可复现的 pytest 用例, 只能由语言先验推断")
    v = per_issue_verify([issue], {"a.py": {1}})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 2


def test_r2_1_path_detector_is_not_triggered_by_prose():
    """R2-1 反向守卫: 纯叙述性文字不能被当成"具体路径"(否则规则失效)。"""
    from pr_review.quality import _looks_like_verification_path

    assert not _looks_like_verification_path("看起来不太对, 属于推测")
    assert not _looks_like_verification_path("无法验证: 该场景难以构造")
    assert _looks_like_verification_path("pytest tests/test_x.py::test_y")
    # 只有 _PATH_SIGNALS 能抓到的形态(无扩展名): 防"信号表被写空也全绿"
    assert _looks_like_verification_path("见 tests/conftest::fixture 的构造")
    assert _looks_like_verification_path("POST /api/x 传 tenant_id=abc")


def test_r2_2_retry_does_not_append_second_user_message():
    """R2-2 回归: 重试只能有一条连续的 user 消息(部分 provider 要求角色交替, 否则 400)。"""
    from pr_review.quality import Judge

    llm = MagicMock()
    llm.chat.side_effect = [
        ChatResponse(content="没有 JSON", model="m", provider="p", usage={}),
        ChatResponse(content='{"score": 80, "verdict": "pass", "reasons": []}',
                     model="m", provider="p", usage={}),
    ]
    jr = Judge(llm=llm, config=QualityConfig(pass_score=70)).evaluate(
        ReviewResult(added_lines={}), "diff"
    )
    assert jr.parse_failed is False
    retry_messages = llm.chat.call_args_list[1][0][0]
    roles = [m["role"] for m in retry_messages]
    assert "user user" not in " ".join(roles), f"出现连续两条 user: {roles}"
    assert roles.count("user") == 1
    assert "只输出一个 JSON" in retry_messages[-1]["content"]  # 指令并入最后一条 user


def test_r2_2_retry_call_failure_is_a_judge_fault_not_a_crash():
    """R2-2 回归: 重试调用本身抛异常(如 provider 400) → 按判官故障返回, 不冒泡打断审查。"""
    from pr_review.quality import Judge

    llm = MagicMock()
    llm.chat.side_effect = [
        ChatResponse(content="没有 JSON", model="m", provider="p", usage={}),
        RuntimeError("400 Bad Request: roles must alternate"),
    ]
    jr = Judge(llm=llm, config=QualityConfig(pass_score=70)).evaluate(
        ReviewResult(added_lines={}), "diff"
    )
    assert jr.parse_failed is True
    assert jr.raw == "没有 JSON"     # 兜底时留档的是首次输出(唯一拿到的那次)


# ---------------------------------------------------------------- 评审 R4-1 回归(2026-09-13)
def test_r4_1_generic_test_tokens_are_not_paths():
    """R4-1 回归: 裸 `test_`/`tests/` 不能算"已给出路径", 否则自认没路径的文本不降级。

    评审给的例子本身是误读(它原本就没被误判), 但同类的真实反例存在——下面这些在修复前
    都会被判成"有路径"→ 不降级 → 门禁级问题继续拦合并(方向与 P0-1 相反)。
    """
    for text in (
        "无法给出可复现的 pytest 用例, 只能由语言先验推断",   # 评审给的例子(钉住, 免得回退)
        "未新增 test_ 用例, 交付前无法验证",
        "没有 tests/ 目录可言, 属推测",
    ):
        v = per_issue_verify([_gate(sev=4, verification=text)], {"a.py": {1}})[0]
        assert v.action == ACTION_DOWNGRADE and v.new_severity == 2, f"{text} 应算自认推演"
        assert "path detector" not in v.reason


def test_r4_1_structured_test_references_are_still_paths():
    """R4-1 反向守卫: 真正的测试引用仍是路径(不能把 test 类一棍子打死)。"""
    for text in (
        "见 tests/conftest::fixture 的构造",
        "对照 tests/test_x.py::test_y (该用例挡不住, 它只覆盖 happy path)",
        "pytest tests/test_x.py -k y",
        # 只有结构化 tests/xxx 正则能命中的形态(无 ::、无 .py、无反引号):
        # 没有这条用例, 那条正则在变异测试里就是空转的
        "见 tests/conftest 的 fixture, 该用例挡不住",
    ):
        v = per_issue_verify([_gate(sev=4, verification=text)], {"a.py": {1}})[0]
        assert v.action == ACTION_KEEP, f"{text} 是具体测试引用, 不该判推演"


def test_r4_1_path_detector_shape():
    """R4-1: 路径判定只认结构化形态(节点 id / 具体文件 / 请求 / 反引号命令 / tests/xxx)。"""
    from pr_review.quality import _looks_like_verification_path as hit

    assert hit("tests/conftest::fixture")
    assert hit("tests/test_x.py")
    assert hit("tests/conftest")          # 无扩展名的测试路径
    assert hit("POST /api/x 传 tenant_id=abc")
    assert hit("`pytest tests/test_x.py -k y`")
    assert not hit("未新增 test_ 用例")
    assert not hit("没有 tests/ 目录")
    assert not hit("pytest 用例拿不出来")
