"""ReviewRunner 编排单测:mock LLM 与 GitHub,零真实调用。"""

from unittest.mock import MagicMock

from gateway import ChatResponse

from pr_review.config import QualityConfig, ReviewConfig
from pr_review.github import PRInfo
from pr_review.review import ReviewIssue, ReviewRunner

PR = PRInfo(
    number=7,
    title="fix: 修复登录",
    body="",
    head_sha="sha-1",
    head_ref="fix/login",
    base_ref="main",
)

# 一个 modified 文件的 GitHub API 返回项
FILE_ITEM = {
    "filename": "src/auth.py",
    "status": "modified",
    "patch": (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -10,3 +10,4 @@\n"
        " def login():\n"
        "-    pwd = request.form['pwd']\n"
        "+    pwd = request.form.get('pwd')\n"
        "     check(pwd)\n"
    ),
}

LLM_OK_JSON = (
    '{"summary": "改动合理,但有安全小隐患。", '
    '"issues": [{"file": "src/auth.py", "line": 11, "severity": "warn", '
    '"title": "建议使用 get 获取参数", "detail": "直接下标可能 KeyError", '
    '"suggestion": "使用 request.form.get"}]}'
)


def _make_runner(*, files=None, llm_responses=None, config=None):
    github = MagicMock()
    github.get_pr_info.return_value = PR
    github.get_pr_files.return_value = [FILE_ITEM] if files is None else files

    llm = MagicMock()
    llm.config.get.return_value = MagicMock(model="deepseek-chat")
    llm.chat.side_effect = [
        ChatResponse(
            content=r,
            model="deepseek-chat",
            provider="deepseek",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            cost=0.001,
        )
        for r in (llm_responses or [LLM_OK_JSON])
    ]
    # 既有测试默认关闭质量门(质量门行为由 test_quality.py 专门覆盖)
    from pr_review.config import QualityConfig

    if config is None:
        config = ReviewConfig(quality_gate=QualityConfig(enabled=False))
    runner = ReviewRunner(github=github, llm=llm, config=config)
    return runner, github, llm


def test_run_extracts_issues():
    runner, github, _ = _make_runner()
    result = runner.run()

    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.file == "src/auth.py"
    assert issue.line == 11
    assert issue.severity == "warn"
    assert result.batches == 1
    assert result.total_cost == 0.001
    # run() 只收集,不发评论(发评论在 main 入口)
    github.post_review.assert_not_called()


def test_main_like_flow_posts_review():
    """模拟 main.py 的流程:run -> format_comment -> post_review。"""
    runner, github, _ = _make_runner()
    result = runner.run()
    body = runner.format_comment(result)
    github.post_review(body=body, head_sha=PR.head_sha)
    github.post_review.assert_called_once_with(body=body, head_sha="sha-1")
    assert "建议使用 get 获取参数" in body
    # 整体判断只提取 summary 字段,不贴原始 JSON
    assert result.summaries == ["改动合理,但有安全小隐患。"]
    assert '{"summary"' not in body


def test_run_skips_ignored_files():
    files = [
        FILE_ITEM,
        {**FILE_ITEM, "filename": "package-lock.json", "patch": "..."},
        {**FILE_ITEM, "filename": "vendor/lib/x.go", "patch": "..."},
    ]
    runner, github, _ = _make_runner(files=files)
    result = runner.run()
    # 只有 src/auth.py 被审查
    assert result.skipped_files == 2
    assert result.batches == 1


def test_run_with_github_api_style_patch():
    """GitHub API 裸 hunk patch(无 diff --git 头)必须正常解析并审查(回归测试)。"""
    bare_patch = "@@ -10,1 +10,2 @@\n def login():\n-    pwd = request.form['pwd']\n+    pwd = request.form.get('pwd')"
    files = [{"filename": "src/auth.py", "status": "modified", "patch": bare_patch}]
    runner, _, llm = _make_runner(files=files)
    result = runner.run()
    assert len(result.issues) == 1
    assert result.issues[0].file == "src/auth.py"
    assert llm.chat.call_count == 1  # 裸 hunk 也被正常送审


def test_run_skips_unparseable_patch():
    """解析不出变更行的 patch 应跳过, 不把空 diff 喂给 LLM。"""
    files = [{"filename": "weird.bin", "status": "modified", "patch": "not-a-diff"}]
    runner, github, llm = _make_runner(files=files)
    result = runner.run()
    assert not result.has_issues
    llm.chat.assert_not_called()
    github.post_review.assert_not_called()


def test_run_empty_candidates_no_review():
    runner, github, _ = _make_runner(files=[])
    result = runner.run()
    assert not result.has_issues
    github.post_review.assert_not_called()


def test_run_filters_below_min_severity():
    cfg = ReviewConfig(min_severity="error", quality_gate=QualityConfig(enabled=False))
    responses = [
        '{"summary": "s", "issues": ['
        '{"file": "a.py", "line": 1, "severity": "warn", "title": "小问题", "detail": "d", "suggestion": "s"},'
        '{"file": "a.py", "line": 2, "severity": "error", "title": "严重问题", "detail": "d", "suggestion": "s"}'
        "]}"
    ]
    runner, _, _ = _make_runner(llm_responses=responses, config=cfg)
    result = runner.run()
    assert [i.title for i in result.issues] == ["严重问题"]


def test_run_batches_files():
    cfg = ReviewConfig(max_files_per_batch=2, quality_gate=QualityConfig(enabled=False))
    files = [{**FILE_ITEM, "filename": f"f{i}.py"} for i in range(5)]
    llm_responses = [LLM_OK_JSON] * 3  # 5 个文件分 3 批
    runner, _, llm = _make_runner(files=files, llm_responses=llm_responses, config=cfg)
    result = runner.run()
    assert result.batches == 3
    assert llm.chat.call_count == 3


def test_format_comment_no_issues():
    runner, _, _ = _make_runner(files=[])
    result = runner.run()
    comment = runner.format_comment(result)
    assert "未发现达到审查门槛的问题" in comment
    assert "✅" in comment


def test_format_comment_numbers_within_each_severity():
    """每个 severity 组内独立编号(error 组 1,2; warn 组重新 1)。"""
    from pr_review.review import ReviewIssue, ReviewResult

    result = ReviewResult(model="deepseek-chat")
    result.issues = [
        ReviewIssue(file="a.py", line=1, severity="error", title="错误1", detail="", suggestion=""),
        ReviewIssue(file="b.py", line=2, severity="error", title="错误2", detail="", suggestion=""),
        ReviewIssue(file="c.py", line=3, severity="warn", title="警告1", detail="", suggestion=""),
    ]
    runner, _, _ = _make_runner(files=[])
    comment = runner.format_comment(result)
    assert "1. **`a.py`:1** — 错误1" in comment
    assert "2. **`b.py`:2** — 错误2" in comment
    assert "1. **`c.py`:3** — 警告1" in comment  # warn 组重新编号


def test_issues_sorted_severity_desc():
    """LLM 返回乱序 severity, run 后按 error > warn > info 排序。"""
    content = (
        '{"summary": "s", "issues": ['
        '{"file": "w.py", "line": 1, "severity": "warn", "title": "w", "detail": "", "suggestion": ""},'
        '{"file": "e.py", "line": 1, "severity": "error", "title": "e", "detail": "", "suggestion": ""},'
        '{"file": "i.py", "line": 1, "severity": "info", "title": "i", "detail": "", "suggestion": ""}'
        "]}"
    )
    runner, _, _ = _make_runner(
        llm_responses=[content],
        config=ReviewConfig(min_severity="info", quality_gate=QualityConfig(enabled=False)),
    )
    result = runner.run()
    assert [i.severity for i in result.issues] == ["error", "warn", "info"]


def test_format_comment_no_issues_fallback_evaluation():
    """无 issues 且 LLM 未给 summary → 兜底给出正向评价。"""
    from pr_review.review import ReviewResult

    result = ReviewResult(model="deepseek-chat")
    runner, _, _ = _make_runner(files=[])
    comment = runner.format_comment(result)
    assert "整体质量良好" in comment
    assert "✅ 未发现达到审查门槛的问题" in comment


def test_format_comment_no_issues_shows_llm_praise():
    """无 issues 且 LLM 给了正向 summary → 评价优先显示。"""
    from pr_review.review import ReviewResult

    result = ReviewResult(model="deepseek-chat")
    result.summaries = ["改动结构清晰, 未发现明显问题"]
    runner, _, _ = _make_runner(files=[])
    comment = runner.format_comment(result)
    assert "改动结构清晰" in comment           # LLM 评价显示
    assert "整体质量良好" not in comment        # 不显示兜底文案
    assert "✅ 未发现达到审查门槛的问题" in comment


def test_format_comment_sections_and_stats():
    cfg = ReviewConfig(show_stats=True, quality_gate=QualityConfig(enabled=False))
    runner, _, _ = _make_runner(config=cfg)
    result = runner.run()
    comment = runner.format_comment(result)
    assert "### 🟡 Warn (1)" in comment
    assert "模型: deepseek-chat" in comment
    assert "成本" in comment


def test_truncate_oversized_file():
    from pr_review.diff import parse_diff

    big_patch = "\n".join(
        [
            "diff --git a/big.py b/big.py",
            "--- a/big.py",
            "+++ b/big.py",
            f"@@ -1,1 +1,{2000} @@",
            *[f"+line{i}" for i in range(2000)],
        ]
    )
    fd = parse_diff(big_patch)[0]
    runner, _, _ = _make_runner(files=[{"filename": "big.py", "status": "modified", "patch": big_patch}])
    runner._truncate_if_too_long(fd, max_lines=100)
    assert sum(len(h.lines) for h in fd.hunks) <= 100 + 1  # +1 为截断提示行
    assert any("文件过大" in l for h in fd.hunks for l in h.lines)


def test_review_issue_from_dict_defaults():
    issue = ReviewIssue.from_dict({"file": "x.py", "title": "t"})
    assert issue.line == 0
    assert issue.severity == "info"
    assert issue.detail == ""
    # v2 新字段默认值
    assert issue.category == "other"
    assert issue.evidence == ""
    assert issue.needs_review is False


# ---------------------------------------------------------------- v2: 新字段/生成代码/上下文
def test_issue_from_dict_v2_fields():
    issue = ReviewIssue.from_dict({
        "file": "x.py", "line": 3, "severity": "error", "title": "t",
        "detail": "d", "suggestion": "s",
        "category": "type_consistency",
        "evidence": "spec 中 authType 类型不一致",
        "needs_review": True,
    })
    assert issue.category == "type_consistency"
    assert "authType" in issue.evidence
    assert issue.needs_review is True


def test_format_comment_shows_evidence_and_review_flag():
    from pr_review.review import ReviewResult

    result = ReviewResult(model="deepseek-chat")
    result.issues = [
        ReviewIssue(
            file="a.py", line=1, severity="error", title="类型不一致",
            detail="d", suggestion="s",
            category="type_consistency", evidence="spec 中 string vs enum", needs_review=True,
        )
    ]
    runner, _, _ = _make_runner(files=[])
    comment = runner.format_comment(result)
    assert "需人工确认" in comment
    assert "📎 依据: spec 中 string vs enum" in comment
    assert "⚠️" in comment


def test_generated_file_skipped(tmp_path):
    """文件头含生成代码标记的跳过审查(噪音 #4 修复)。"""
    gen = tmp_path / "generated.ts"
    gen.write_text(
        "// Generated by orval\n// DO NOT EDIT\nexport const x = 1;\n",
        encoding="utf-8",
    )
    files = [{
        "filename": "generated.ts", "status": "modified",
        "patch": "@@ -1,1 +1,1 @@\n+export const x = 1;",
    }]
    github = MagicMock()
    github.get_pr_info.return_value = PR
    github.get_pr_files.return_value = files
    llm = MagicMock()
    llm.config.get.return_value = MagicMock(model="deepseek-chat")
    llm.chat.side_effect = [
        ChatResponse(content=LLM_OK_JSON, model="m", provider="p", usage={})
    ]
    runner = ReviewRunner(github=github, llm=llm, config=ReviewConfig(), repo_root=tmp_path)
    result = runner.run()
    assert not result.has_issues
    assert result.skipped_files == 1
    llm.chat.assert_not_called()


def test_context_injected_into_prompt():
    """repo_context 注入 build_messages 的 user 消息。"""
    from pr_review.diff import parse_diff
    from pr_review.prompt import build_messages

    fd = parse_diff(FILE_ITEM["patch"])[0]
    msgs = build_messages(PR, [fd], ReviewConfig(), repo_context="spec 中定义接口契约")
    assert "spec 中定义接口契约" in msgs[1]["content"]
    assert "仓库上下文" in msgs[1]["content"]
    # 不注入时没有该段
    msgs2 = build_messages(PR, [fd], ReviewConfig())
    assert "仓库上下文" not in msgs2[1]["content"]


# ---------------------------------------------------------------- 行内评论线程
def test_build_inline_comments_basic():
    """issue 行号落在 diff 新增行 → 生成行内评论线程。"""
    runner, _, _ = _make_runner()  # FILE_ITEM: src/auth.py 新增行 11
    result = runner.run()
    comments = runner.build_inline_comments(result)
    assert len(comments) == 1
    c = comments[0]
    assert c["path"] == "src/auth.py"
    assert c["line"] == 11
    assert c["side"] == "RIGHT"
    assert "建议使用 get 获取参数" in c["body"]
    assert "💡" in c["body"]


def test_build_inline_comments_skips_unchanged_line():
    """行号不在新增行集合(LLM 给了上下文行)→ 降级留整体评论, 不建线程。"""
    responses = [
        '{"summary": "s", "issues": [{"file": "src/auth.py", "line": 10, '
        '"severity": "warn", "title": "上下文行问题", "detail": "d", "suggestion": "s"}]}'
    ]
    runner, _, _ = _make_runner(llm_responses=responses)
    result = runner.run()
    assert len(result.issues) == 1
    assert runner.build_inline_comments(result) == []  # 行 10 是上下文行


def test_build_inline_comments_skips_no_line():
    """line=0(无法定位)→ 降级。"""
    responses = [
        '{"summary": "s", "issues": [{"file": "src/auth.py", "severity": "info", '
        '"title": "无行号问题", "detail": "d", "suggestion": "s"}]}'
    ]
    runner, _, _ = _make_runner(llm_responses=responses)
    result = runner.run()
    assert runner.build_inline_comments(result) == []


def test_inline_comment_has_main_comment_ref():
    """行内线程正文引用主评论的分组编号(2026-08-14 用户反馈: 线程与主评论无法匹配)。"""
    responses = [
        '{"summary": "s", "issues": [{"file": "src/auth.py", "line": 11, '
        '"severity": "warn", "title": "线程问题", "detail": "d", "suggestion": "s"}]}'
    ]
    runner, _, _ = _make_runner(llm_responses=responses)
    result = runner.run()
    comments = runner.build_inline_comments(result)
    assert "对应整体评论 **Warn #1**" in comments[0]["body"]


def test_inline_comment_ref_aligns_when_earlier_issue_skipped():
    """第一个 issue 无行号(留整体评论), 第二个可定位 → 线程引用应为 #2 而非 #1。"""
    responses = [
        '{"summary": "s", "issues": ['
        '{"file": "src/auth.py", "severity": "warn", "title": "无行号问题", "detail": "d", "suggestion": "s"},'
        '{"file": "src/auth.py", "line": 11, "severity": "warn", "title": "可定位问题", "detail": "d", "suggestion": "s"}'
        "]}"
    ]
    runner, _, _ = _make_runner(llm_responses=responses)
    result = runner.run()
    comments = runner.build_inline_comments(result)
    assert len(comments) == 1
    assert "对应整体评论 **Warn #2**" in comments[0]["body"]


def test_inline_body_includes_evidence_and_review_flag():
    from pr_review.review import ReviewResult

    result = ReviewResult()
    result.issues = [
        ReviewIssue(
            file="a.py", line=1, severity="error", title="类型不一致", detail="d",
            suggestion="s", evidence="spec 中 string vs enum", needs_review=True,
        )
    ]
    runner, _, _ = _make_runner(files=[])
    body = runner._inline_body(result.issues[0])
    assert "需人工确认" in body
    assert "spec 中 string vs enum" in body
    assert "🔴" in body


# ---------------------------------------------------------------- 评审次数显示
def test_format_comment_shows_review_number():
    from pr_review.review import ReviewResult

    result = ReviewResult(model="deepseek-chat", review_no=3)
    runner, _, _ = _make_runner(files=[])
    comment = runner.format_comment(result)
    assert "第 3 次评审" in comment
    assert comment.startswith("## 🤖 AI 代码审查 · 第 3 次评审")


def test_format_comment_no_number_when_unknown():
    from pr_review.review import ReviewResult

    result = ReviewResult(model="deepseek-chat")  # review_no 默认 0
    runner, _, _ = _make_runner(files=[])
    assert "次评审" not in runner.format_comment(result)


# ---------------------------------------------------------------- P1a 决议驱动
def test_collect_handled_from_threads():
    """扫描线程提取决议标记 → 已处理清单。"""
    from pr_review.review import ReviewRunner

    github = MagicMock()
    github.get_pull_comments.return_value = [
        {"body": "明白\n\n<!-- pr-review:ignore:src/auth.py:11 -->"},
        {"body": "已确认\n\n<!-- pr-review:resolve:src/api/client.ts:42 -->"},
        {"body": "普通评论没有标记"},
    ]
    llm = MagicMock()
    runner = ReviewRunner(github=github, llm=llm, config=ReviewConfig())
    handled = runner._collect_handled()
    assert ("src/auth.py", 11) in handled
    assert ("src/api/client.ts", 42) in handled
    assert len(handled) == 2


def test_collect_handled_disabled_by_config():
    from pr_review.review import ReviewRunner

    github = MagicMock()
    runner = ReviewRunner(
        github=github, llm=MagicMock(), config=ReviewConfig(resolve_enabled=False)
    )
    assert runner._collect_handled() == []
    github.get_pull_comments.assert_not_called()


def test_extract_issues_filters_handled():
    """已处理清单命中 → 确定性过滤(即使 LLM 重复报了)。"""
    content = (
        '{"summary": "s", "issues": ['
        '{"file": "src/auth.py", "line": 11, "severity": "warn", "title": "重复问题", "detail": "d", "suggestion": "s"},'
        '{"file": "src/other.py", "line": 3, "severity": "warn", "title": "新问题", "detail": "d", "suggestion": "s"}'
        "]}"
    )
    runner, _, _ = _make_runner(files=[])
    issues = runner._extract_issues(content, handled=[("src/auth.py", 11)])
    assert len(issues) == 1
    assert issues[0].file == "src/other.py"


def test_handled_injected_into_prompt():
    """已处理清单注入 user prompt 的独立段落。"""
    from pr_review.diff import parse_diff
    from pr_review.prompt import build_messages

    fd = parse_diff(FILE_ITEM["patch"])[0]
    msgs = build_messages(PR, [fd], ReviewConfig(), handled=[("src/auth.py", 11)])
    user = msgs[1]["content"]
    assert "已处理清单" in user
    assert "src/auth.py:11" in user
    assert "不要重复报" in user
    # 不传 handled 时无该段落
    assert "已处理清单" not in build_messages(PR, [fd], ReviewConfig())[1]["content"]


# ---------------------------------------------------------------- 解析失败链路(2026-08-14 事故修复)
BAD_JSON = '{"summary": "s", "issues": [{"file": "a.py", "title": "未终止'  # 截断的 JSON


def test_review_batch_retries_bad_json_then_succeeds():
    """chat 返回非法 JSON → 预检重试 → 第二次成功, 不误判。"""
    runner, _, llm = _make_runner(llm_responses=[BAD_JSON, LLM_OK_JSON])
    result = runner.run()
    assert result.parse_errors == []          # 重试后成功, 无解析错误
    assert len(result.issues) == 1            # 正常提取 issues
    assert llm.chat.call_count == 2           # 原审 + 1 次重试


def test_parse_error_marks_and_skips_quality_gate():
    """重试耗尽仍非法 → parse_errors 标记 + 跳过质量门(judge 不调用), 不再静默 pass。"""
    from unittest.mock import patch

    from pr_review.quality import QualityConfig as QG  # noqa: F401 (仅占位)

    runner, _, llm = _make_runner(
        llm_responses=[BAD_JSON] * 4,  # 原审 + 重试都非法
        config=ReviewConfig(quality_gate=QualityConfig(enabled=True)),
    )
    with patch("pr_review.quality.Judge.evaluate") as mocked_judge:
        result = runner.run()
    assert len(result.parse_errors) >= 1     # 失败被显式标记
    assert result.quality_verdict == "degraded"  # 走降级通道(不 pass)
    assert result.issues == []               # 无 issues 可发布
    mocked_judge.assert_not_called()         # 关键: 不评估"失败产物"


def test_format_parse_failed_comment():
    from pr_review.review import ReviewResult

    result = ReviewResult(model="deepseek-chat", review_no=2)
    result.parse_errors = ["Unterminated string starting at: line 54 column 17"]
    runner, _, _ = _make_runner(files=[])
    comment = runner.format_parse_failed_comment(result)
    assert "输出解析失败" in comment
    assert "第 2 次评审" in comment
    assert "Unterminated string" in comment
    assert "rerun" in comment


# ---------------------------------------------------------------- locations 多位置(2026-08-14 合并契约)
def test_from_dict_parses_locations():
    """locations 字段解析 + 单位置兼容。"""
    d = {
        "file": "a.py", "line": 10, "severity": "warn", "title": "t", "detail": "d", "suggestion": "s",
        "locations": [{"file": "b.py", "line": 22}, {"file": "c.py", "line": 30}],
    }
    issue = ReviewIssue.from_dict(d)
    assert issue.file == "a.py" and issue.line == 10
    assert issue.all_locations() == [("a.py", 10), ("b.py", 22), ("c.py", 30)]


def test_all_locations_dedupe_and_skip_invalid():
    """主位置与 locations 重复/无效条目去重。"""
    d = {
        "file": "a.py", "line": 10, "severity": "warn", "title": "t", "detail": "d", "suggestion": "s",
        "locations": [{"file": "a.py", "line": 10}, {"file": "b.py", "line": 0}, {"file": "", "line": 5}],
    }
    issue = ReviewIssue.from_dict(d)
    assert issue.all_locations() == [("a.py", 10)]  # 重复 + 无效都被过滤


def test_format_comment_multi_location():
    """合并 issue 的主评论显示全部位置(逗号分隔)。"""
    responses = [(
        '{"summary": "s", "issues": [{"file": "a.py", "line": 1, "severity": "warn", '
        '"title": "契约不一致", "detail": "d", "suggestion": "s", '
        '"locations": [{"file": "b.py", "line": 2}, {"file": "c.py", "line": 3}]}]}'
    )]
    runner, _, _ = _make_runner(llm_responses=responses)
    result = runner.run()
    comment = runner.format_comment(result)
    assert "`a.py`:1, `b.py`:2, `c.py`:3" in comment


def test_inline_comments_multi_location():
    """多位置 issue → 每个位置一个线程, 引用同编号 + 位置序号。"""
    responses = [(
        '{"summary": "s", "issues": [{"file": "src/auth.py", "line": 11, "severity": "warn", '
        '"title": "契约不一致", "detail": "d", "suggestion": "s", '
        '"locations": [{"file": "src/auth.py", "line": 13}]}]}'
    )]
    runner, _, _ = _make_runner(llm_responses=responses)
    result = runner.run()
    result.added_lines = {"src/auth.py": {11, 13}}  # 手工补充新增行集合
    comments = runner.build_inline_comments(result)
    assert len(comments) == 2
    assert "对应整体评论 **Warn #1** · 位置 1/2" in comments[0]["body"]
    assert "对应整体评论 **Warn #1** · 位置 2/2" in comments[1]["body"]
    assert comments[0]["line"] == 11 and comments[1]["line"] == 13
