"""质量门(P1b): LLM-as-judge 打分 + 零成本结构校验层。

详设: docs/pr-review-quality-gate.md
- judge 独立 prompt(rubric 四维度), 模型可独立配置(复用同一 LLMClient, chat 传 model 覆盖)
- 结构校验(零成本): 行号 ∈ diff 新增行 / severity 合法, 作为 judge 参考信号(不直接否决)
- 输出: {"score": 0-100, "verdict": "pass|rewrite", "reasons": [...]}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import QualityConfig, SEVERITIES
from .prompt import parse_review_json

JUDGE_SYSTEM_PROMPT = """你是代码审查质量评估员,对 AI 审查产出打分。
评分维度(rubric):
- 准确性: issue 是否真实对应代码问题(幻觉/误报)— 对照 diff 与 evidence 判断
- 可操作性: 修改建议是否具体可执行(是否有明确改动方向/代码示意)
- 覆盖度: 关键改动是否被审到(漏报)— 对照 diff 中的核心变更
- 噪音: 是否过度挑剔无关紧要的问题

评分规则:
- 若 issues 为空数组:
  - diff 主要是不影响逻辑的文件(文档/配置/生成内容)→ 空 issues 属**正确审查**, 应给 pass(≥ {pass_score})
  - diff 含实质代码变更, 且明显存在应审出的缺陷 → 才判 rewrite(漏报), 并说明漏掉了什么
- 总分 ≥ {pass_score} → verdict: "pass"
- 总分低于 → verdict: "rewrite", 并给出 reasons(逐条说明扣分点, 供重写时作为反馈)

输出严格 JSON(不要多余文本):
{{"score": 0-100, "verdict": "pass" 或 "rewrite", "reasons": ["扣分点1", "扣分点2"]}}
"""


@dataclass
class JudgeResult:
    score: int = 0
    verdict: str = "rewrite"  # pass / rewrite
    reasons: list[str] = field(default_factory=list)


class Judge:
    """独立质检员:对审查产出打分,不达标返回可执行的重写反馈。"""

    def __init__(self, llm: Any, config: QualityConfig):
        self.llm = llm
        self.config = config

    def evaluate(self, result: Any, diff_text: str) -> JudgeResult:
        """对一次审查结果打分。

        result: ReviewResult(duck type, 访问 .issues/.added_lines/.quality_reasons)
        diff_text: 全部候选文件的紧凑 diff 文本(judge 核对准确性用)
        """
        signals = structural_signals(result.issues, result.added_lines)
        messages = build_judge_messages(result, diff_text, signals, self.config)
        kwargs: dict[str, Any] = {"max_tokens": 400}
        if self.config.judge_model:
            kwargs["model"] = self.config.judge_model
        # 模型路由: judge 可独立 provider(如便宜模型), 审查主流程用默认 provider
        if self.config.judge_provider:
            kwargs["provider"] = self.config.judge_provider
        resp = self.llm.chat(messages, **kwargs)
        return self._parse(resp.content)

    @staticmethod
    def _parse(content: str) -> JudgeResult:
        try:
            data = parse_review_json(content)
            score = int(data.get("score", 0) or 0)
            verdict = str(data.get("verdict", "rewrite")).lower()
            if verdict not in ("pass", "rewrite"):
                verdict = "rewrite"
            reasons = [str(r) for r in (data.get("reasons") or [])]
            return JudgeResult(score=score, verdict=verdict, reasons=reasons)
        except ValueError:
            return JudgeResult(score=0, verdict="rewrite", reasons=["judge 输出无法解析"])


def structural_signals(
    issues: list[Any], added_lines: dict[str, set[int]]
) -> list[str]:
    """零成本结构校验,产出 judge 参考信号(不直接否决 LLM 输出)。

    校验项: 行号缺失 / 行号不在 diff 新增行(疑似幻觉) / severity 越界(1-5)。
    """
    signals: list[str] = []
    for issue in issues:
        if not issue.line:
            signals.append(f"{issue.file}: 行号缺失(无法定位到 diff 行)")
        elif added_lines.get(issue.file) and issue.line not in added_lines[issue.file]:
            signals.append(f"{issue.file}:{issue.line} 不在 diff 新增行(疑似幻觉)")
    bad = [i.file for i in issues if not (1 <= i.severity <= 5)]
    if bad:
        signals.append(f"{len(bad)} 条 issue severity 越界(应 1-5): {set(bad)}")
    return signals


def build_judge_messages(
    result: Any,
    diff_text: str,
    signals: list[str],
    config: QualityConfig,
) -> list[dict[str, str]]:
    """组装 judge 的 messages: issues + diff + 结构信号 + 上轮反馈。"""
    diff = diff_text[: config.max_judge_input_chars]
    issues_json = json.dumps(
        [asdict(i) for i in result.issues], ensure_ascii=False, indent=1
    )
    parts = [
        "## AI 审查产出的 issues",
        issues_json,
        "## 对应文件 diff(截断)",
        f"```diff\n{diff}\n```",
    ]
    if signals:
        parts += ["## 结构校验信号(供评分参考)", "\n".join(f"- {s}" for s in signals)]
    if result.quality_reasons:
        parts += ["## 上一轮 judge 反馈(重写轮)", "\n".join(f"- {r}" for r in result.quality_reasons)]
    system = JUDGE_SYSTEM_PROMPT.format(pass_score=config.pass_score)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
