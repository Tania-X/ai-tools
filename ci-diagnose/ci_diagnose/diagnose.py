"""诊断核心:日志提取截断 + LLM 诊断 prompt + 输出解析。

- 日志聚焦:命中 error/failed 关键字的行(±2 行) + 尾部 30 行,控制注入 token
- 输出 JSON: summary / root_cause / location / suggestion / fix(可选)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .prompt import build_diagnose_messages

logger = logging.getLogger("ci_diagnose")

# 日志中命中即视为"相关行"的关键字(小写匹配)
LOG_KEYWORDS = (
    "error", "failed", "fatal", "exception", "traceback", "fail",
    "not found", "cannot", "unable", "panic", "exit status",
)


def extract_relevant_log(log: str, max_chars: int = 8000) -> str:
    """从完整日志提取诊断相关片段:错误行(±2) + 尾部,总长控制在 max_chars。"""
    lines = log.splitlines()
    if not lines:
        return ""

    # 1. 命中错误关键字的行 ±2
    hits: list[int] = []
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(k in low for k in LOG_KEYWORDS):
            hits.append(idx)
    relevant: list[str] = []
    for i in hits:
        relevant.extend(lines[max(0, i - 2) : min(len(lines), i + 3)])

    # 2. 尾部(失败堆栈通常在末尾)
    tail = lines[-30:]

    # 3. 去重保序 + 拼接
    seen: set[str] = set()
    combined: list[str] = []
    for line in relevant + tail:
        if line not in seen:
            seen.add(line)
            combined.append(line)

    text = "\n".join(combined)
    return text[-max_chars:] if len(text) > max_chars else text


def parse_diagnose_json(content: str) -> dict[str, str]:
    """容错解析诊断 JSON(容忍 ```json 围栏/多余文本)。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"诊断输出中未找到 JSON: {content[:200]!r}")
    data = json.loads(text[start : end + 1])
    return {
        key: str(data.get(key, "")).strip()
        for key in ("summary", "root_cause", "location", "suggestion", "fix")
    }


class Diagnoser:
    """用 LLM 分析 CI 失败日志,产出结构化诊断。"""

    def __init__(self, llm: Any, *, max_log_chars: int = 8000):
        self.llm = llm
        self.max_log_chars = max_log_chars

    def diagnose(self, workflow_name: str, run_id: int, log_text: str) -> dict[str, str]:
        """诊断失败日志;解析失败时返回降级结果。"""
        excerpt = extract_relevant_log(log_text, self.max_log_chars)
        messages = build_diagnose_messages(workflow_name, run_id, excerpt)
        resp = self.llm.chat(messages, max_tokens=600)
        try:
            return parse_diagnose_json(resp.content)
        except ValueError as e:
            logger.warning("诊断输出解析失败: %s", e)
            return {
                "summary": "诊断输出解析失败(日志片段仍附在下方供人工查看)",
                "root_cause": "",
                "location": "",
                "suggestion": "请人工查看日志原文。",
                "fix": "",
            }
