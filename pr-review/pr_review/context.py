"""仓库上下文收集器。

第一轮 review 评估(devops-dashboard PR #2)结论:2/3 误报根因是 AI 只看 diff、
看不到项目约定与后端契约。本模块在审查前自动收集 AGENTS.md / README / docs / spec
等约定文件,注入 prompt,让 AI 结合契约与设计意图下结论。

设计:
- glob 匹配(config.context_files),文件缺失静默跳过
- 每个文件只读前若干行(避免 spec 等大文件爆 token),总预算限制(config.max_context_chars)
- 按配置顺序累计,超预算即停止(优先保留排在前面的约定文件)
"""

from __future__ import annotations

import glob
import logging
from pathlib import Path

logger = logging.getLogger("pr_review.context")

# 单文件最多读取的行数
MAX_LINES_PER_FILE = 150


class ContextCollector:
    """收集仓库约定/契约文件内容,生成注入 prompt 的文本块。"""

    def __init__(
        self,
        repo_root: str | Path = ".",
        *,
        patterns: list[str] | None = None,
        max_chars: int = 8000,
    ):
        self.repo_root = Path(repo_root)
        self.patterns = patterns or []
        self.max_chars = max_chars

    def collect(self) -> str:
        """返回上下文文本(空串表示无可收集内容)。"""
        files = self._resolve_files()
        if not files:
            return ""

        parts: list[str] = []
        used = 0
        for path in files:
            content = self._read_head(path)
            if not content:
                continue
            block = f"### {path}\n```\n{content}\n```"
            if used + len(block) > self.max_chars:
                remain = self.max_chars - used
                if remain > 200:  # 剩余预算太少就不塞了
                    parts.append(block[:remain].rstrip() + "\n```")
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ 内部
    def _resolve_files(self) -> list[Path]:
        """按 glob 展开文件列表(保持配置顺序,去重)。"""
        seen: set[Path] = set()
        result: list[Path] = []
        for pattern in self.patterns:
            for raw in glob.glob(str(self.repo_root / pattern), recursive=True):
                p = Path(raw)
                if not p.is_file():
                    continue
                key = p.resolve()
                if key in seen:
                    continue
                seen.add(key)
                result.append(p)
        return result

    @staticmethod
    def _read_head(path: Path) -> str:
        """读文件前 MAX_LINES_PER_FILE 行,异常(编码/权限)静默返回空。"""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[:MAX_LINES_PER_FILE]
        except OSError:
            logger.debug("读取上下文文件失败(跳过): %s", path)
            return ""
        return "".join(lines).strip()
