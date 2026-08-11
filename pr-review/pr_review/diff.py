"""Unified diff 解析与切片(零第三方依赖)。

GitHub API 的 /pulls/{n}/files 返回每个文件的 ``patch`` 字段,
格式为 unified diff 片段,本模块负责解析出行级变更供 prompt 使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# @@ -old_start,old_count +new_start,new_count @@ 可选 hunk 标题
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffHunk:
    """单个 hunk:变更块在旧文件/新文件的起始行号与原始行。"""

    old_start: int
    new_start: int
    lines: list[str] = field(default_factory=list)  # 原始行(含 +/- 前缀)

    def added_lines(self) -> list[tuple[int, str]]:
        """新增行 [(新文件行号, 内容), ...]"""
        result: list[tuple[int, str]] = []
        new_no = self.new_start
        for line in self.lines:
            if line.startswith("+"):
                result.append((new_no, line[1:]))
                new_no += 1
            elif line.startswith("-"):
                pass
            else:  # 上下文行(含 '\' 续行标记)
                new_no += 1
        return result

    def removed_lines(self) -> list[tuple[int, str]]:
        """删除行 [(旧文件行号, 内容), ...]"""
        result: list[tuple[int, str]] = []
        old_no = self.old_start
        for line in self.lines:
            if line.startswith("-"):
                result.append((old_no, line[1:]))
                old_no += 1
            elif line.startswith("+"):
                pass
            else:
                old_no += 1
        return result


@dataclass
class FileDiff:
    """一个文件的变更。"""

    path: str  # 新路径(删除文件可能为空)
    old_path: str
    status: str  # added / modified / removed / renamed / copied
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def added(self) -> list[tuple[int, str]]:
        """全部新增行 [(新行号, 内容), ...]"""
        out: list[tuple[int, str]] = []
        for hunk in self.hunks:
            out.extend(hunk.added_lines())
        return out

    @property
    def removed(self) -> list[tuple[int, str]]:
        """全部删除行 [(旧行号, 内容), ...]"""
        out: list[tuple[int, str]] = []
        for hunk in self.hunks:
            out.extend(hunk.removed_lines())
        return out

    @property
    def line_count(self) -> int:
        """变更行数(新增 + 删除),用于估算 token 与切片。"""
        return len(self.added) + len(self.removed)

    def to_display_lines(self, context: int = 3) -> list[str]:
        """转成带行号的展示文本(新增行标 +, 删除行标 -, 上下文行标空格)。"""
        out: list[str] = []
        for hunk in self.hunks:
            out.append(
                f"@@ -{hunk.old_start} +{hunk.new_start} @@"
            )
            old_no, new_no = hunk.old_start, hunk.new_start
            for line in hunk.lines:
                prefix = line[:1] if line[:1] in "+-" else " "
                if line.startswith("+"):
                    out.append(f"{new_no:>6} + {line[1:]}")
                    new_no += 1
                elif line.startswith("-"):
                    out.append(f"{old_no:>6} - {line[1:]}")
                    old_no += 1
                else:
                    out.append(f"{old_no:>6}   {line}")
                    old_no += 1
                    new_no += 1
        return out


def _parse_hunk_header(line: str) -> tuple[int, int] | None:
    m = _HUNK_RE.match(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(3))


def parse_diff(patch: str) -> list[FileDiff]:
    """解析 unified diff 文本(支持单文件片段或完整 diff)。"""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: DiffHunk | None = None

    for raw in patch.splitlines():
        line = raw

        if line.startswith("diff --git "):
            # 先从头提取路径(rename 场景 a/old -> b/new;删除/新增时 +++/--- 为 /dev/null 不覆盖)
            m = re.match(r"^diff --git a/(.*?) b/(.*)$", line)
            current = FileDiff(
                path=_strip_a_b(m.group(2)) if m else "",
                old_path=_strip_a_b(m.group(1)) if m else "",
                status="modified",
            )
            files.append(current)
            current_hunk = None
            continue

        if line.startswith("--- "):
            if current is not None:
                current.old_path = _strip_a_b(line[4:])  # 含 /dev/null(新增文件)
            continue

        if line.startswith("+++ "):
            if current is not None:
                path = _strip_a_b(line[4:])
                if path != "/dev/null":  # 删除文件时新路径是 /dev/null,不覆盖
                    current.path = path
            continue

        if line.startswith("@@ "):
            header = _parse_hunk_header(line)
            if header is None:
                continue
            if current is None:
                # 裸 hunk(无 diff --git 头):GitHub REST API 的 patch 字段即此格式,
                # 自动创建文件上下文,避免 hunk 被丢弃导致审查拿到空 diff
                current = FileDiff(path="", old_path="", status="modified")
                files.append(current)
            current_hunk = DiffHunk(old_start=header[0], new_start=header[1])
            current.hunks.append(current_hunk)
            continue

        if current_hunk is not None:
            current_hunk.lines.append(line)

    return files


def _strip_a_b(path: str) -> str:
    """去掉 '--- a/xxx' / '+++ b/xxx' 前缀,处理 '/dev/null' 与引号。"""
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path
