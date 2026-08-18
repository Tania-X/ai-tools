"""repo_tools 工具集单测: 读写/搜索/语法查询/路径安全。"""

from __future__ import annotations

import pytest

from pr_review.repo_tools import RepoTools, RepoToolError, TOOL_SCHEMAS


@pytest.fixture
def repo(tmp_path):
    """构造一个小仓库: src/main.go(函数+调用) + src/util.go + README.md。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.go").write_text(
        "package main\n\n"
        "// foo 计算 x+1\n"
        "func foo(x int) int { return x + 1 }\n\n"
        "// bar 调用 foo\n"
        "func bar() int { return foo(1) }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "util.go").write_text(
        "package main\n\n"
        "// util 工具函数\n"
        "func util(x int) int { return x * 2 }\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# demo\nusage: call foo\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- read_file
def test_read_file_basic(repo):
    tools = RepoTools(repo)
    out = tools.read_file("src/main.go")
    assert "func foo(x int) int { return x + 1 }" in out
    assert "src/main.go" in out


def test_read_file_with_range(repo):
    tools = RepoTools(repo, max_file_lines=1)
    out = tools.read_file("src/main.go", start_line=1, end_line=2)
    assert "package main" in out
    assert "func foo" not in out  # 第 4 行, 不在 1-2 范围


def test_read_file_truncates(repo):
    tools = RepoTools(repo, max_file_lines=1)
    out = tools.read_file("src/main.go")
    assert "已截断" in out  # 超过 max_file_lines 提示


def test_read_file_missing(repo):
    tools = RepoTools(repo)
    # 模型侧走 execute 入口, 错误转字符串不抛异常
    assert "文件不存在" in tools.execute("read_file", {"path": "nope.go"})


# ---------------------------------------------------------------- grep
def test_grep_finds_reference(repo):
    tools = RepoTools(repo)
    out = tools.grep("foo")
    assert "src/main.go:4: func foo" in out or "foo" in out
    assert "src/util.go" not in out  # util.go 无 foo


def test_grep_scoped_to_path(repo):
    tools = RepoTools(repo)
    out = tools.grep("main", path="src")
    assert "main.go:1: package main" in out.replace("\\", "/")


def test_grep_invalid_regex(repo):
    tools = RepoTools(repo)
    assert "正则无效" in tools.execute("grep", {"pattern": "([unclosed"})


# ---------------------------------------------------------------- ast_grep
def test_ast_grep_finds_func_defs(repo):
    tools = RepoTools(repo)
    out = tools.ast_grep("func $F($$$)", language="go")
    assert "foo" in out
    assert "bar" in out
    assert "util" in out


def test_ast_grep_finds_calls(repo):
    tools = RepoTools(repo)
    out = tools.ast_grep("$C($$$)", language="go")
    assert "foo(1)" in out  # bar 里调用 foo


def test_ast_grep_scoped_to_file(repo):
    tools = RepoTools(repo)
    out = tools.ast_grep("func $F($$$)", language="go", path="src/util.go")
    assert "util" in out
    assert "foo" not in out  # 限定 util.go


# ---------------------------------------------------------------- list_dir
def test_list_dir(repo):
    tools = RepoTools(repo)
    out = tools.list_dir("src")
    assert "main.go" in out
    assert "util.go" in out


# ---------------------------------------------------------------- 安全
def test_path_traversal_blocked(repo):
    tools = RepoTools(repo)
    assert "越界" in tools.execute("read_file", {"path": "../outside.go"})
    assert "越界" in tools.execute("list_dir", {"path": "../../etc"})


def test_unknown_tool(repo):
    tools = RepoTools(repo)
    assert "未知工具" in tools.execute("rm_rf", {})


def test_tool_schemas_have_four_tools():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == {"read_file", "grep", "ast_grep", "list_dir"}
