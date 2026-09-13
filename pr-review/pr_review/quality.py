"""质量门(P1b): LLM-as-judge 打分 + 零成本结构校验层。

详设: docs/pr-review-quality-gate.md
- judge 独立 prompt(rubric 四维度), 模型可独立配置(复用同一 LLMClient, chat 传 model 覆盖)
- 结构校验(零成本): 行号 ∈ diff 新增行 / severity 合法, 作为 judge 参考信号(不直接否决)
- 输出: {"score": 0-100, "verdict": "pass|rewrite", "reasons": [...]}
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import QualityConfig, SEVERITIES
from .prompt import parse_review_json

logger = logging.getLogger(__name__)

# P0-3: judge 输出无法解析时的一次严格重试指令
RETRY_JSON_INSTRUCTION = (
    "上一次输出无法解析为 JSON。请只输出一个 JSON 对象, 不要任何解释文字、"
    'Markdown 围栏或前后缀, 形如 {"score": 0-100, "verdict": "pass|rewrite", "reasons": [...]}。'
)

JUDGE_SYSTEM_PROMPT = """你是代码审查质量评估员,对 AI 审查产出打分。
评分维度(rubric):
- 准确性: issue 是否真实对应代码问题(幻觉/误报)— 对照 diff 与 evidence 判断
- 可操作性: 修改建议是否具体可执行(是否有明确改动方向/代码示意)
- 覆盖度: 关键改动是否被审到(漏报)— 对照 diff 中的核心变更
- 噪音: 是否过度挑剔无关紧要的问题
- 严重度与证据匹配(2026-08-19 P1): issue 的 trigger/impact 判断是否与 evidence 一致。
  证据是假设性故障("若 X 失败则…")却判 high/real → 严重度高判(最贵的误报, 会误拦合并), 必须扣分。
- 可验证性(P0-1, 2026-08-13): 会拦合并的 issue(≥4)是否给了可验证路径(verification):
  复现输入/命令、能反驳它的现有测试, 或明确标注"属推演"。verification 为空且 evidence 也是空话,
  或自认属推演却判 ≥4 → 扣分(不可证伪的结论会误拦合并)。

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
    # P0-3(判官故障留档): 输出无法解析时 parse_failed=True, raw 留原始输出(截断)。
    # 与"judge 正常给出低分"必须区分: 前者是工具故障(不可知), 后者是真实判负。
    parse_failed: bool = False
    raw: str = ""


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
        parsed = self._parse(resp.content)
        if not parsed.parse_failed:
            return parsed
        # P0-3: 解析失败先重试一次(明确要求纯 JSON 输出, 不带解释/围栏)。
        # 线上故障(x2 次/15 轮)多为"输出前后带说明文字"导致, 一次严格重试即可救回。
        logger.warning(
            "judge 输出无法解析, 重试一次(原文前 200 字): %s",
            (resp.content or "")[:200],
        )
        retry_messages = messages + [
            {"role": "user", "content": RETRY_JSON_INSTRUCTION},
        ]
        retry = self.llm.chat(retry_messages, **kwargs)
        parsed_retry = self._parse(retry.content)
        if not parsed_retry.parse_failed:
            logger.info("judge 严格重试成功")
            return parsed_retry
        logger.error(
            "judge 连续 2 次输出无法解析, 本轮质量评分不可用(原文留档前 500 字): %s",
            (retry.content or resp.content or "")[:500],
        )
        return parsed_retry

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
            return JudgeResult(
                score=0, verdict="rewrite",
                reasons=["judge 输出无法解析(judge 故障, 非审查结论)"],
                parse_failed=True, raw=(content or "")[:500],
            )


def structural_signals(
    issues: list[Any], added_lines: dict[str, set[int]]
) -> list[str]:
    """零成本结构校验,产出 judge 参考信号(不直接否决 LLM 输出)。

    校验项: 行号缺失 / 行号不在 diff 新增行(疑似幻觉) / severity 越界(1-5) /
    严重度高判(2026-08-19 P1: 证据是假设性故障但级别 ≥4) /
    可验证性(P0-1, 2026-09-13: ≥3 缺 evidence / ≥4 无任何事实支点 / 自认推演却判 ≥4)。
    """
    signals: list[str] = []
    for issue in issues:
        if not issue.line:
            signals.append(f"{issue.file}: 行号缺失(无法定位到 diff 行)")
        elif added_lines.get(issue.file) and issue.line not in added_lines[issue.file]:
            signals.append(f"{issue.file}:{issue.line} 不在 diff 新增行(疑似幻觉)")
        # P0-1 规则一(报告): severity ≥3 必须有 evidence(file:line + 代码片段)。
        # 3 级本就不阻塞, 故只出信号给 judge, 不降级(降 3→2 会白丢"必修"标记)
        if issue.severity >= 3 and not str(getattr(issue, "evidence", "") or "").strip():
            signals.append(
                f"{issue.file}:{issue.line} 级别 {issue.severity}(≥3)缺判断依据(evidence 为空)"
            )
        # 门禁级问题缺任何事实支点(P0-1): 无 verification 且无 evidence → 提示 judge。
        # 注意只靠"verification 字段为空"不报: evidence 本身可能就是验证路径,
        # 那样会把确定性证据的真问题(如"第21行必有 nil")一起标成可疑, 属噪音。
        if issue.severity >= 4 and _has_no_basis(issue):
            signals.append(
                f"{issue.file}:{issue.line} 门禁级(≥4)既无可验证路径也无判断依据(疑似推演)"
            )
        # 自认可验证路径属推演却判门禁级 → 提示 judge
        if issue.severity >= 4 and _verification_is_speculative(issue):
            signals.append(
                f"{issue.file}:{issue.line} 自认可验证路径属推演, 但级别为 {issue.severity}(≥4 会拦合并)"
            )
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


_HYPOTHETICAL_MARKERS = (
    "若", "如果", "可能", "一旦", "万一", "假设", "hypothetical",
    "失败时", "异常时", "失败会", "若失败", "如果失败", "可能导致", "无法保证",
)


def _looks_hypothetical(issue: Any) -> bool:
    """issue 的描述/依据是否呈假设性(高判风险信号, 供质量门降级与 judge 复核)。"""
    hay = f"{issue.detail} {issue.evidence} {issue.suggestion}"
    return any(m in hay for m in _HYPOTHETICAL_MARKERS)


# P0-1: verification 里"自认可验证路径属推演"的标记词
_SPECULATION_MARKERS = (
    "推演", "推测", "无法验证", "无验证路径", "猜测", "n/a",
)
# 英文逃生口 "none" 只认短形式(文档约定为 "none: 属推演"):
# 否则 "None of the existing tests cover this" 这类说明会被误判成推演, 把真问题降级
_MAX_NONE_FORM_LEN = 20


def _verification_is_speculative(issue: Any) -> bool:
    """LLM 是否在 verification 里自认"拿不出验证路径"(P0-1 的确定性止损口)。"""
    v = str(getattr(issue, "verification", "") or "").strip().lower()
    if not v:
        return False
    if any(m in v for m in _SPECULATION_MARKERS):
        return True
    return v.startswith("none") and len(v) <= _MAX_NONE_FORM_LEN


def _has_no_basis(issue: Any) -> bool:
    """门禁级问题既无可验证路径、也无判断依据 → 典型的严重度高判(报告分类 B)。"""
    v = str(getattr(issue, "verification", "") or "").strip()
    e = str(getattr(issue, "evidence", "") or "").strip()
    return not v and not e


def _is_severity_high_judgement(issue: Any) -> bool:
    """严重度高判判定(2026-08-24 三信号 + 2026-09-13 P0-1 两信号, 零成本确定性规则):

    severity ≥4(会拦合并, 最贵的误报) 且命中任一:
      a. 文本呈假设性措辞(若…失败/可能/万一...)
      b. LLM 自标 trigger=hypothetical(两轴事实)
      c. 纯约定违反(category=convention): 策略锚点"明确约定违反=3", 不应到 4
      d. verification 自认"属推演"(P0-1): 自认拿不出验证路径
      e. 无 verification 且无 evidence(P0-1): 无任何事实支点
    命中 → 降级(a-c/e 到 3; d 到 2, 见 per_issue_verify 分档)。
    """
    if int(getattr(issue, "severity", 0) or 0) < 4:
        return False
    if _looks_hypothetical(issue):
        return True
    if str(getattr(issue, "trigger", "") or "").strip().lower() == "hypothetical":
        return True
    if str(getattr(issue, "category", "") or "").strip().lower() == "convention":
        return True
    # d. 自认可验证路径属推演(none: 属推演)——自认无法证明, 不应拦合并
    if _verification_is_speculative(issue):
        return True
    # e. 既无可验证路径也无依据: 无任何事实支点, 不应拦合并
    if _has_no_basis(issue):
        return True
    return False


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
    4. 假设性证据 / LLM 自标 trigger=hypothetical / 纯约定违反 /
       门禁级(≥4)但既无 verification 又无 evidence → downgrade 到 3(必修不阻塞)
    4b. 自认可验证路径属推演(none: 属推演)+ 级别 ≥4 → downgrade 到 2(轻微)
       - 报告 P0-1 指定; 与 severity 映射表"无路径→轻微"一致
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

        # 4. 严重度高判(≥4, 会拦合并) → 降级到 3(必修不阻塞)
        #    三信号: 假设性措辞 / trigger=hypothetical / category=convention(策略锚点约定违反=3)
        if _is_severity_high_judgement(issue):
            reason = f"严重度高判(级别 {sev} ≥4 会拦合并)"
            # 目标级别按信号强弱分档:
            #   自认"属推演" → 2(轻微): 连路径都拿不出, 映射表里"无路径"就该是轻微级
            #   其余(假设性措辞/约定违反/无依据) → 3(必修不阻塞): 保留可见性但解除阻塞
            # 优先级(自上而下, 前者命中即定档): 约定违反(策略锚点=3) >
            #   自认推演(2) > 假设性措辞/trigger=hypothetical/无依据(3)
            target = 3
            if getattr(issue, "category", "") == "convention":
                reason += ": 纯约定违反(策略锚点=3)"
            elif _verification_is_speculative(issue):
                reason += ": 自认可验证路径属推演(P0-1 无可验证路径)"
                target = 2
            elif _looks_hypothetical(issue):
                reason += ": 证据/描述呈假设性故障"
            elif getattr(issue, "trigger", "") == "hypothetical":
                reason += ": LLM 自标 trigger=hypothetical"
            elif _has_no_basis(issue):
                reason += ": 既无可验证路径也无判断依据"
            reason += f", 降级到 {target}"
            verdicts.append(
                IssueVerdict(issue=issue, action=ACTION_DOWNGRADE,
                             reason=reason, new_severity=target)
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
