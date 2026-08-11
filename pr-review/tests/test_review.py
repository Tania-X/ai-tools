"""ReviewRunner 编排单测:mock LLM 与 GitHub,零真实调用。"""

from unittest.mock import MagicMock

from gateway import ChatResponse

from pr_review.config import ReviewConfig
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
    runner = ReviewRunner(github=github, llm=llm, config=config or ReviewConfig())
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
    cfg = ReviewConfig(min_severity="error")
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
    cfg = ReviewConfig(max_files_per_batch=2)
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


def test_format_comment_sections_and_stats():
    cfg = ReviewConfig(show_stats=True)
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
