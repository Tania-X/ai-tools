"""MCP server: 仓库代码访问工具集(agentic-production 探索, 2026-08-16)。

把 pr-review 的 repo_tools 4 工具暴露为标准 MCP 协议(stdio transport),
让 Claude Code / Codex / 任意 MCP 客户端可调用——自研工具与主流 agent 生态互操作。

运行(需先 pip install fastmcp):
    REPO_ROOT=<仓库根> python mcp_server.py

MCP 客户端配置示例(Claude Code 的 .mcp.json / 通用 mcpServers):
{
  "mcpServers": {
    "repo-tools": {
      "command": "python",
      "args": ["<本文件绝对路径>"],
      "env": { "REPO_ROOT": "/path/to/repo" }
    }
  }
}

工具(与 pr_review.repo_tools 完全一致, 含路径越界防护与输出截断):
- read_file: 读文件(行范围/截断)
- grep: 正则搜索(找引用/定义)
- ast_grep: 语法感知查询(函数定义/调用点)
- list_dir: 目录浏览
"""

from __future__ import annotations

import os
from pathlib import Path

from fastmcp import FastMCP

from pr_review.repo_tools import RepoTools


def create_server(repo_root: str | Path) -> FastMCP:
    """构造 MCP server, 绑定指定仓库根(便于测试注入)。"""
    tools = RepoTools(repo_root)
    mcp = FastMCP(
        "repo-tools",
        instructions=(
            "仓库代码访问工具集: 读取文件、正则搜索、语法感知查询(AST)、目录浏览。"
            "所有路径为相对仓库根路径, 越界访问会被拒绝。"
        ),
    )

    @mcp.tool()
    def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """读取仓库内文件内容(相对仓库根路径), 可按行号截取范围; 文件过大时返回开头部分并提示分段读取。"""
        return tools.read_file(path, start_line, end_line)

    @mcp.tool()
    def grep(pattern: str, path: str | None = None, max_results: int = 50) -> str:
        """在仓库内搜索文本/正则, 返回匹配的文件:行号和该行内容; 用于找符号引用、调用处、定义位置。"""
        return tools.grep(pattern, path, max_results)

    @mcp.tool()
    def ast_grep(
        pattern: str,
        language: str | None = None,
        path: str | None = None,
        max_results: int = 50,
    ) -> str:
        """语法感知查询(基于 AST, tree-sitter): 找函数定义/调用点/类型声明等结构化代码。pattern 用 ast-grep 语法, 如 'func $F($$$)'。"""
        return tools.ast_grep(pattern, language, path, max_results)

    @mcp.tool()
    def list_dir(path: str = ".") -> str:
        """列出目录内容(相对仓库根), 了解项目结构。"""
        return tools.list_dir(path)

    return mcp


def main() -> None:
    root = os.environ.get("REPO_ROOT") or "."
    create_server(root).run(transport="stdio")


if __name__ == "__main__":
    main()
