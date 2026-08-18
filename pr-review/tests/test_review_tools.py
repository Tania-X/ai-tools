"""agentic 审查工具循环测试: 工具执行/消息回填/上限。"""

from __future__ import annotations

import json
import pytest

from gateway import ChatResponse
from pr_review.config import QualityConfig, ReviewConfig
from pr_review.github import PRInfo
from pr_review.review import ReviewIssue, ReviewRunner

PR = PRInfo(
    number=7,
    title="fix: 登录",
    body="",
    head_sha="sha-1",
    head_ref="fix/login",
    base_ref="main",
)

FILE_ITEM = {
    "filename": "src/auth.go",
    "status": "modified",
    "patch": (
        "diff --git a/src/auth.go b/src/auth.go\n"
        "--- a/src/auth.go\n"
        "+++ b/src/auth.go\n"
        "@@ -10,3 +10,4 @@\n"
        " func login() {\n"
        "+    // 调用 foo\n"
        "     foo()\n"
    ),
}

GOOD_JSON = '{"summary": "s", "issues": [{"file": "src/auth.go", "line": 11, "severity": "warn", "title": "t", "detail": "d", "suggestion": "s"}]}'


def _resp(content: str = "", tool_calls: list | None = None) -> ChatResponse:
    return ChatResponse(content=content, model="m", provider="p", usage={}, cost=0.0, tool_calls=tool_calls)


def _chat_msgs(llm, idx: int) -> list:
    """取第 idx 次 chat 调用的 messages(位置参数)。"""
    args, kwargs = llm.chat.call_args_list[idx]
    return kwargs.get("messages", args[0])


def _make_runner(tmp_path, chat_side_effect):
    from unittest.mock import MagicMock

    github = MagicMock()
    github.get_pr_info.return_value = PR
    github.get_pr_files.return_value = [FILE_ITEM]
    llm = MagicMock()
    llm.chat.side_effect = chat_side_effect
    config = ReviewConfig(quality_gate=QualityConfig(enabled=False))
    return ReviewRunner(github=github, llm=llm, config=config, repo_root=tmp_path), llm


def test_tool_loop_executes_tool_and_returns(tmp_path):
    """模型先请求工具, 工具结果回填后模型给出最终 JSON。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.go").write_text(
        "package main\n\nfunc foo() {}\nfunc login() { foo() }\n", encoding="utf-8"
    )
    tool_call = {
        "id": "call_1",
        "name": "grep",
        "arguments": json.dumps({"pattern": "foo"}),
    }
    runner, llm = _make_runner(tmp_path, [_resp(tool_calls=[tool_call]), _resp(GOOD_JSON)])
    result = runner.run()
    assert llm.chat.call_count == 2
    # 第二次调用的 messages 应包含 tool 回填
    msgs = _chat_msgs(llm, 1)
    assert any(m.get("role") == "tool" for m in msgs)
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert "foo" in tool_msg["content"]  # grep 结果
    # 结果正常
    assert len(result.issues) == 1
    assert result.issues[0].severity == 2


def test_tool_loop_passes_tools_schema_when_repo_available(tmp_path):
    runner, llm = _make_runner(tmp_path, [_resp(GOOD_JSON)])
    runner.run()
    _, kwargs = llm.chat.call_args
    assert "tools" in kwargs
    names = {t["function"]["name"] for t in kwargs["tools"]}
    assert names == {"read_file", "grep", "ast_grep", "list_dir"}


def test_tool_loop_disabled_without_repo_root():
    """repo_root 为 None 时不注入工具(退化为纯 prompt)。"""
    from unittest.mock import MagicMock

    github = MagicMock()
    github.get_pr_info.return_value = PR
    github.get_pr_files.return_value = [FILE_ITEM]
    llm = MagicMock()
    llm.chat.return_value = _resp(GOOD_JSON)
    runner = ReviewRunner(
        github=github, llm=llm,
        config=ReviewConfig(quality_gate=QualityConfig(enabled=False)),
    )
    runner.run()
    _, kwargs = llm.chat.call_args
    assert kwargs.get("tools") is None


def test_tool_loop_hard_limit_raises(tmp_path):
    """模型持续请求工具 → 超 max_tool_calls 抛 ToolLoopError(不静默跳过)。"""
    from pr_review.review import ToolLoopError

    tool_call = {"id": "c", "name": "list_dir", "arguments": "{}"}
    runner, llm = _make_runner(tmp_path, [_resp(tool_calls=[tool_call])] * 20)
    with pytest.raises(ToolLoopError, match="工具调用超上限"):
        runner.run()


def test_tool_error_feedback_not_crash(tmp_path):
    """工具执行出错时结果带错误说明回填, 不中断审查。"""
    tool_call = {"id": "c", "name": "grep", "arguments": json.dumps({"pattern": "([bad"})}
    runner, llm = _make_runner(tmp_path, [_resp(tool_calls=[tool_call]), _resp(GOOD_JSON)])
    result = runner.run()
    _, kwargs = llm.chat.call_args_list[1]
    tool_msg = next(m for m in _chat_msgs(llm, 1) if m.get("role") == "tool")
    assert "正则无效" in tool_msg["content"]
    assert len(result.issues) == 1
