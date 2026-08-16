"""MCP server 集成测试: 工具注册 + stdio 协议级调用。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import create_server


def _repo(tmp_path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.go").write_text(
        "package main\n\nfunc foo() int { return 1 }\nfunc bar() int { return foo() }\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(coro):
    # asyncio.run 每次新建事件循环, 避免 pytest 下复用已关闭 loop 导致 BrokenResourceError
    return asyncio.run(coro)


def test_server_registers_four_tools(tmp_path):
    mcp = create_server(_repo(tmp_path))
    for name in ["read_file", "grep", "ast_grep", "list_dir"]:
        assert mcp.get_tool(name) is not None, f"工具 {name} 未注册"


def _stdio_session_cm(repo: Path):
    """起子进程 stdio server 的嵌套 context(与手动验证一致, 避免手动 __aenter__ 顺序问题)。"""
    from contextlib import asynccontextmanager
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def _cm():
        server = str(Path(__file__).parent.parent / "mcp_server.py")
        params = StdioServerParameters(
            command=sys.executable,
            args=[server],
            env={**os.environ, "REPO_ROOT": str(repo)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    return _cm()


def test_stdio_server_list_and_grep(tmp_path):
    async def go():
        async with _stdio_session_cm(_repo(tmp_path)) as session:
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {"read_file", "grep", "ast_grep", "list_dir"}
            result = await session.call_tool("grep", {"pattern": "func foo"})
            text = "".join(c.text or "" for c in result.content)
            assert "main.go" in text and "func foo" in text

    _run(go())


def test_stdio_server_read_file(tmp_path):
    async def go():
        async with _stdio_session_cm(_repo(tmp_path)) as session:
            result = await session.call_tool("read_file", {"path": "src/main.go"})
            text = "".join(c.text or "" for c in result.content)
            assert "func foo" in text

    _run(go())
