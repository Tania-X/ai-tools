"""仓库代码访问工具集(agentic 审查用, 函数调用白名单)。

对照 CodeRabbit 的沙箱 CLI 能力(cat / grep / ast-grep / ls)映射为受控函数:
- read_file: 读文件(支持行范围, 截断)
- grep: 正则搜索(找引用/定义)
- ast_grep: 语法感知查询(函数定义/调用点, tree-sitter)
- list_dir: 目录浏览

安全约束:
- 所有路径经 realpath 校验, 必须落在 repo_root 内(防路径穿越)
- 输出截断: 单文件行数 / 结果条数 / 总字符数上限, 防 token 爆炸
- ast-grep 不可用时(未安装 ast-grep-py)返回明确提示, 不抛异常
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("pr_review.repo_tools")

# ast-grep 语言名映射(扩展名 → tree-sitter language)
_LANG_BY_EXT = {
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".py": "python",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".kt": "kotlin",
}

# 工具 schema(注入 LLM 的 tools 数组)
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内文件内容(相对仓库根路径)。可按行号截取范围; 文件超过上限时只返回开头部分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根的路径, 如 backend/internal/authz/authz.go"},
                    "start_line": {"type": "integer", "description": "起始行号(1 起, 可选)"},
                    "end_line": {"type": "integer", "description": "结束行号(可选)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "在仓库内搜索文本/正则, 返回匹配的文件:行号和该行内容。用于找符号引用、调用处、定义位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "限定搜索的目录/文件(相对仓库根, 可选)"},
                    "max_results": {"type": "integer", "description": "最多返回条数(默认 50)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ast_grep",
            "description": "语法感知查询(基于 AST)。用于找函数定义、调用点、类型声明等结构化代码。pattern 用 ast-grep 语法, 如 'func $F($$$)' 找 Go 函数定义、'$C($$$)' 找调用。未安装 ast-grep 时返回提示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "ast-grep pattern, 如 'func $F($$$)'"},
                    "language": {"type": "string", "description": "语言(go/typescript/tsx/python 等); 不传则按 path 扩展名推断"},
                    "path": {"type": "string", "description": "限定搜索的目录/文件(相对仓库根, 可选); 不传则全仓库"},
                    "max_results": {"type": "integer", "description": "最多返回条数(默认 50)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容(相对仓库根), 了解项目结构。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根的目录(默认 '.')"},
                },
            },
        },
    },
]


class RepoTools:
    def __init__(
        self,
        repo_root: str | Path,
        *,
        max_file_lines: int = 200,
        max_result_chars: int = 6000,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.max_file_lines = max_file_lines
        self.max_result_chars = max_result_chars

    # ------------------------------------------------------------------ 入口
    def execute(self, name: str, args: dict[str, Any]) -> str:
        """按名称分发工具调用, 返回结果文本(截断)。未知工具/异常返回错误信息(不抛)。"""
        try:
            if name == "read_file":
                return self.read_file(**args)
            if name == "grep":
                return self.grep(**args)
            if name == "ast_grep":
                return self.ast_grep(**args)
            if name == "list_dir":
                return self.list_dir(**args)
            return f"未知工具: {name}"
        except RepoToolError as e:
            return f"工具错误: {e}"
        except Exception as e:  # 兜底, 工具失败不中断审查
            logger.warning("工具 %s 执行异常: %s", name, e)
            return f"工具执行异常: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ 工具
    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        p = self._safe_path(path)
        if not p.is_file():
            raise RepoToolError(f"文件不存在: {path}")
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError as e:
            raise RepoToolError(f"读取失败: {e}") from e

        total = len(lines)
        if start_line is None and end_line is None:
            lines = lines[: self.max_file_lines]
            truncated = total > self.max_file_lines
        else:
            s = (start_line or 1) - 1
            e = end_line or total
            lines = lines[max(0, s) : min(e, total)]
            truncated = False

        body = "\n".join(lines)
        if len(body) > self.max_result_chars:
            body = body[: self.max_result_chars]
            truncated = True
        note = f" (文件共 {total} 行)"
        if truncated:
            note += " — 已截断, 可用 start_line/end_line 分段读取"
        return f"{path}{note}:\n```\n{body}\n```"

    def grep(self, pattern: str, path: str | None = None, max_results: int = 50) -> str:
        base = self._safe_path(path) if path else self.repo_root
        if not base.exists():
            raise RepoToolError(f"路径不存在: {path or '.'}")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise RepoToolError(f"正则无效: {e}") from e

        hits: list[str] = []
        for p in self._iter_files(base):
            try:
                for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{p.relative_to(self.repo_root)}:{lineno}: {line.strip()[:120]}")
                        if len(hits) >= max_results:
                            return self._fmt_hits(hits, max_results, truncated=True)
            except OSError:
                continue
        return self._fmt_hits(hits, max_results, truncated=False)

    def ast_grep(self, pattern: str, language: str | None = None, path: str | None = None, max_results: int = 50) -> str:
        try:
            from ast_grep_py import Pattern, SgRoot
        except ImportError:
            return "ast-grep 未安装(需 pip install ast-grep-py), 请改用 grep/read_file"

        lang = language or "go"
        targets = self._ast_targets(path, lang)
        if not targets:
            return "没有匹配该语言的代码文件"

        rule = {"rule": {"pattern": pattern}}
        hits: list[str] = []
        for p in targets:
            try:
                code = p.read_text(encoding="utf-8", errors="ignore")
                if not code.strip():
                    continue
                root = SgRoot(code, lang)
                for node in root.root().find_all(Pattern(rule)):
                    rng = node.range()
                    hits.append(f"{p.relative_to(self.repo_root)}:{rng.start.line + 1}: {node.text()[:120]}")
                    if len(hits) >= max_results:
                        return self._fmt_hits(hits, max_results, truncated=True)
            except Exception:
                continue  # 单个文件解析失败跳过
        return self._fmt_hits(hits, max_results, truncated=False)

    def list_dir(self, path: str = ".") -> str:
        base = self._safe_path(path)
        if not base.is_dir():
            raise RepoToolError(f"目录不存在: {path}")
        entries = []
        for item in sorted(base.iterdir()):
            if item.name.startswith(".git"):
                continue
            kind = "DIR" if item.is_dir() else f"{item.stat().st_size}B"
            entries.append(f"{kind:>10}  {item.name}")
        body = "\n".join(entries[:100]) if entries else "(空目录)"
        if len(entries) > 100:
            body += f"\n... 共 {len(entries)} 项"
        return f"{path or '.'}/:\n{body}"

    # ------------------------------------------------------------------ 内部
    def _safe_path(self, rel: str) -> Path:
        """解析相对仓库根的路径并校验在根内(防 ../ 穿越)。"""
        p = (self.repo_root / rel).resolve()
        if p != self.repo_root and self.repo_root not in p.parents:
            raise RepoToolError(f"路径越界(禁止访问仓库外): {rel}")
        return p

    def _iter_files(self, base: Path) -> list[Path]:
        """递归列出代码/文本文件(排除 .git / 二进制 / 大型目录)。"""
        result: list[Path] = []
        skip_dirs = {".git", "node_modules", "vendor", "dist", "build", ".venv", "__pycache__"}
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in skip_dirs for part in p.relative_to(base).parts):
                continue
            if p.stat().st_size > 512 * 1024:  # 512KB 以上视为大文件/二进制
                continue
            result.append(p)
        return result

    def _ast_targets(self, path: str | None, lang: str) -> list[Path]:
        base = self._safe_path(path) if path else self.repo_root
        if base.is_file():
            return [base] if _LANG_BY_EXT.get(base.suffix, "") == lang else []
        if not base.is_dir():
            return []
        return [p for p in self._iter_files(base) if _LANG_BY_EXT.get(p.suffix, "") == lang]

    def _fmt_hits(self, hits: list[str], max_results: int, truncated: bool) -> str:
        if not hits:
            return "(无匹配结果)"
        body = "\n".join(hits)
        if len(body) > self.max_result_chars:
            body = body[: self.max_result_chars]
            truncated = True
        if truncated:
            body += f"\n... 已截断(最多 {max_results} 条)"
        return body


class RepoToolError(Exception):
    """工具执行错误(路径越界/参数无效等), 结果直接反馈给模型。"""


def tools_to_json() -> str:
    return json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
