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
- 严重度与证据匹配(2026-08-19 P1): issue 的 trigger/impact 判断是否与 evidence 一致。
  证据是假设性故障("若 X 失败则…")却判 high/real → 严重度高判(最贵的误报, 会误拦合并), 必须扣分。

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

    校验项: 行号缺失 / 行号不在 diff 新增行(疑似幻觉) / severity 越界(1-5) /
    严重度高判(2026-08-19 P1: 证据是假设性故障但级别 ≥4)。
    """
    signals: list[str] = []
    for issue in issues:
        if not issue.line:
            signals.append(f"{issue.file}: 行号缺失(无法定位到 diff 行)")
        elif added_lines.get(issue.file) and issue.line not in added_lines[issue.file]:
            signals.append(f"{issue.file}:{issue.line} 不在 diff 新增行(疑似幻觉)")
        # 严重度高判: 假设性故障证据 + 高级别(≥4 会拦合并, 最贵的误报)
        if issue.severity >= 4 and _looks_hypothetical(issue):
            signals.append(
                f"{issue.file}:{issue.line} 疑似严重度高判: 证据/描述是假设性故障"
                f"(若…失败/可能/如果), 但级别为 {issue.severity}(≥4 会拦合并)"
            )
    bad = [i.file for i in issues if not (1 <= i.severity <= 5)]
    if bad:
        signals.append(f"{len(bad)} 条 issue severity 越界(应 1-5): {set(bad)}")
    return signals


_HYPOTHETICAL_MARKERS = ("若", "如果", "可能", "一旦", "假设", "失败时", "异常时", "hypothetical")


def _looks_hypothetical(issue: Any) -> bool:
    """issue 的描述/依据是否呈假设性(高判风险信号, 供 judge 复核)。"""
    hay = f"{issue.detail} {issue.evidence} {issue.suggestion}"
    return any(m in hay for m in _HYPOTHETICAL_MARKERS)


# ---------------------------------------------------------------------------
# 逐条验证层(2026-08-20 质量门改造, 建议 1-3 落地)
# 定位: 在 LLM judge 整批打分之前, 先用零成本确定性规则逐条处理 issue。
# 对的不动, 错的单独处理; 删除/降级比例过高才触发整批重写(降级哨兵)。
# ---------------------------------------------------------------------------

# 逐条处理的动作
ACTION_KEEP = "keep"        # 保留(未发现问题)
ACTION_DELETE = "delete"    # 删除(幻觉/行号缺失/证据不成立)
ACTION_DOWNGRADE = "downgrade"  # 降级(假设性证据 + 高级别, 按策略降到 3)
ACTION_FIX = "fix"          # 修正(severity 越界等确定性修复)

# 降级哨兵阈值: 删除+降级比例超过该值 → 判定本轮审查整体质量差
SENTINEL_THRESHOLD = 0.30


@dataclass
class IssueVerdict:
    """单条 issue 的逐条验证结论。"""

    issue: Any
    action: str = ACTION_KEEP
    reason: str = ""
    new_severity: int = 0  # 仅 ACTION_DOWNGRADE / ACTION_FIX 时有效


def per_issue_verify(issues: list[Any], added_lines: dict[str, set[int]]) -> list[IssueVerdict]:
    """确定性逐条验证(零成本, 不调 LLM)。

    规则(对应 docs/pr-review-quality-gate.md §4 与 review-severity-policy skill):
    1. 行号缺失(0 或 None) → delete(无法定位到 diff 行, 疑似幻觉)
    2. 行号不在 diff 新增行 → delete(疑似幻觉; 只评本 PR 引入的问题)
    3. severity 越界(非 1-5) → fix(钳制到合法范围)
    4. 假设性证据 + 级别 ≥4 → downgrade 到 3(最高级误报: 会拦合并)
       - 级别由两轴事实映射, 假设性触发最多 3(必修不阻塞)
    5. 其余 → keep

    返回 verdict 列表(与 issues 一一对应)。
    """
    verdicts: list[IssueVerdict] = []
    for issue in issues:
        sev = int(getattr(issue, "severity", 0) or 0)

        # 1. 行号缺失
        line = int(getattr(issue, "line", 0) or 0)
        if line <= 0:
            verdicts.append(IssueVerdict(issue=issue, action=ACTION_DELETE, reason="行号缺失, 无法定位到 diff 行"))
            continue

        # 2. 行号不在 diff 新增行(幻觉)
        file = getattr(issue, "file", "") or ""
        if added_lines.get(file) and line not in added_lines[file]:
            verdicts.append(
                IssueVerdict(issue=issue, action=ACTION_DELETE,
                             reason=f"{file}:{line} 不在 diff 新增行(疑似幻觉, 只评本 PR 引入的问题)")
            )
            continue

        # 3. severity 越界
        if not (1 <= sev <= 5):
            clamped = max(1, min(5, sev))
            verdicts.append(
                IssueVerdict(issue=issue, action=ACTION_FIX,
                             reason=f"severity {sev} 越界(应 1-5), 钳制为 {clamped}",
                             new_severity=clamped)
            )
            continue

        # 4. 假设性证据 + 高级别(≥4) → 降级到 3(必修不阻塞)
        if sev >= 4 and _looks_hypothetical(issue):
            verdicts.append(
                IssueVerdict(issue=issue, action=ACTION_DOWNGRADE,
                             reason=f"证据是假设性故障(若…失败/可能/如果)但级别为 {sev}(≥4 会拦合并), 降级到 3",
                             new_severity=3)
            )
            continue

        # 5. 其余保留
        verdicts.append(IssueVerdict(issue=issue, action=ACTION_KEEP))

    return verdicts


def apply_verdicts(verdicts: list[IssueVerdict]) -> list[Any]:
    """按 verdict 执行: 删除的剔除, 降级/修正的改 severity, 保留的不动。"""
    kept: list[Any] = []
    for v in verdicts:
        if v.action == ACTION_DELETE:
            continue
        if v.action in (ACTION_DOWNGRADE, ACTION_FIX) and v.new_severity:
            v.issue.severity = v.new_severity
        kept.append(v.issue)
    return kept


def sentinel_triggered(verdicts: list[IssueVerdict]) -> bool:
    """降级哨兵: 删除+降级比例 > SENTINEL_THRESHOLD → 本轮审查整体质量差, 触发整批重写。"""
    if not verdicts:
        return False
    touched = sum(1 for v in verdicts if v.action in (ACTION_DELETE, ACTION_DOWNGRADE))
    return (touched / len(verdicts)) > SENTINEL_THRESHOLD


def verdict_summary(verdicts: list[IssueVerdict]) -> str:
    """人类可读的验证摘要(日志/降级评论用)。"""
    parts = [f"共 {len(verdicts)} 条 issue:"]
    for v in verdicts:
        loc = f"{getattr(v.issue, 'file', '')}:{getattr(v.issue, 'line', '')}"
        parts.append(f"  [{v.action}] {loc} — {v.reason}")
    return "\n".join(parts)


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
