"""质量门(P1b): LLM-as-judge 打分 + 零成本结构校验层。

详设: docs/pr-review-quality-gate.md
- judge 独立 prompt(rubric 四维度), 模型可独立配置(复用同一 LLMClient, chat 传 model 覆盖)
- 结构校验(零成本): 行号 ∈ diff 新增行 / severity 合法, 作为 judge 参考信号(不直接否决)
- 输出: {"score": 0-100, "verdict": "pass|rewrite", "reasons": [...]}
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import CATEGORY_TOOL_DIMENSION, QualityConfig, SEVERITIES
from .prompt import parse_review_json

logger = logging.getLogger(__name__)

#: 从"无法解析的 judge 输出"里找 reasons 数组。
#: **不能要求收尾的 `]`**: 线上两轮故障的真实形态是 judge 输出被 max_tokens 截断,
#: 也就是 JSON 根本没写完(`{"score": 58, ... "reasons": ["严重度高判: ...` 断在半句)。
#: 所以先定位 `"reasons": [` 的开头, 再往后取(遇到 `]` 就截到那里)。
_REASONS_HEAD = re.compile(r'"reasons"\s*:\s*\[', re.S)
_REASONS_ITEM = re.compile(r'"((?:[^"\\]|\\.){5,})"')
MAX_SALVAGED_REASONS = 3


def _salvage_reasons(content: str, limit: int = MAX_SALVAGED_REASONS) -> list[str]:
    """Best-effort: pull the judge's reasons out of unparseable output.

    Only used on the failure path, and every caller labels the result as
    "extracted from the raw output" — the point is to keep the judge's
    disagreement visible, never to pass it off as a structured verdict.
    """
    match = _REASONS_HEAD.search(content or "")
    if not match:
        return []
    tail = content[match.end():]
    closing = tail.find("]")
    body = tail[:closing] if closing != -1 else tail   # 截断时没有 ]
    reasons: list[str] = []
    for raw_item in _REASONS_ITEM.findall(body):
        text = raw_item.replace('\\"', '"').replace("\\\\", "\\").strip()
        if text and text not in reasons:
            reasons.append(text)
        if len(reasons) >= limit:
            break
    return reasons

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
- 竞态类断言(2026-09-16): "两个请求/两个线程可能同时…"、"若关停发生在构建窗口内…"这类
  **没有具体交错或复现路径**的并发结论若被判 ≥4, 同样属严重度高判, 必须扣分并写明"缺确定性交错"。
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
    # 判官故障时的"抢救信息"(2026-09-16): judge 解析失败 ≠ 它没说话。它往往在散文里
    # 写了一个完整 JSON(或至少 reasons 列表), 而那些话正是它与 reviewer 的分歧所在
    # (线上两轮故障里 judge 都在说"reviewer 严重度高判")。丢掉它们等于丢掉最有价值的信号,
    # 所以从原文里宽松提取, 并**显式标注为原文提取**, 不与结构化结果混淆。
    salvaged_reasons: list[str] = field(default_factory=list)


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
        signals = structural_signals(
            result.issues, result.added_lines,
            getattr(result, "verified_dimensions", None),
        )
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
        # R2-2(评审第 2 轮): 不能直接 append 第二条 user 消息——部分 provider(Anthropic 格式
        # 端点)要求 user/assistant 交替, 连续两条 user 会被 400 拒掉; 故并入最后一条 user 消息。
        retry_messages = messages[:-1] + [
            {
                "role": "user",
                "content": (messages[-1]["content"] if messages else "")
                + "\n\n" + RETRY_JSON_INSTRUCTION,
            },
        ]
        try:
            retry = self.llm.chat(retry_messages, **kwargs)
        except Exception as e:  # noqa: BLE001 重试失败按"判官故障"处理, 不冒泡打断整轮审查
            logger.error("judge 重试调用失败, 按判官故障处理: %s", e)
            return parsed
        parsed_retry = self._parse(retry.content)
        if not parsed_retry.parse_failed:
            logger.info("judge 严格重试成功")
            return parsed_retry
        # R1-4(评审第 1 轮): 两次原文都要留档——首次输出往往才是真实故障形态,
        # 只留重试那次会让"日志过期后无法复盘"。
        if parsed_retry.raw:
            parsed_retry.raw = (
                f"[first]\n{parsed.raw}\n[retry]\n{parsed_retry.raw}"
            )
        logger.error(
            "judge 连续 2 次输出无法解析, 本轮质量评分不可用(两次原文留档): %s",
            parsed_retry.raw[:500],
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
                salvaged_reasons=_salvage_reasons(content),
            )


def structural_signals(
    issues: list[Any],
    added_lines: dict[str, set[int]],
    verified_dimensions: dict[str, str] | None = None,
) -> list[str]:
    """零成本结构校验,产出 judge 参考信号(不直接否决 LLM 输出)。

    校验项: 行号缺失 / 行号不在 diff 新增行(疑似幻觉) / severity 越界(1-5) /
    严重度高判(2026-08-19 P1: 证据是假设性故障但级别 ≥4) /
    可验证性(P0-1, 2026-09-13: ≥3 缺 evidence / ≥4 无任何事实支点 / 自认推演却判 ≥4) /
    职责边界(P0-5, 2026-09-13: 落在机器已确认的维度上却未说明工具缺口)。
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
        # 机器已确认维度上仍报问题却未说明工具缺口(P0-5) → 提示 judge 复核
        if _is_toolchain_covered_no_gap(issue, verified_dimensions or {}):
            dimension = CATEGORY_TOOL_DIMENSION.get(
                str(getattr(issue, "category", "") or "").strip().lower(), ""
            )
            signals.append(
                f"{issue.file}:{issue.line} 落在机器已确认的 {dimension} 维度上, 但未说明工具缺口"
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
    "推演", "推测", "无法验证", "无验证路径", "猜测",
    # 同义的自认说法(2026-09-13 评审 R2: 只覆盖"无法验证"会漏掉这些常见写法)
    "无法给出", "无法提供", "拿不出", "无法复现", "无复现路径",
)
# 具体验证路径的特征(2026-09-13 评审 R2-1): 只要 verification 里给出了**具体可复现的东西**,
# 就不能再判成"自认推演"——一个通用词(如 n/a)或一句补充说明, 不能盖过已经给出的路径。
# 方向性理由: 误判成推演会降到 2 并解除阻塞, 属"把真问题放走"; 反之只是多留一条问题。
#
# 只认**结构化**形态(评审 R4-1): 裸子串 `test_` / `tests/` 会把"未新增 test_ 用例,
# 交付前无法验证"这类**自认没路径**的文本误判成"已给出路径" → 不降级(方向反了)。
# 所以 test 类只认带路径段的 `tests/xxx`。
_PATH_SIGNALS = ("::", "http://", "https://")
_PATH_PATTERNS = (
    re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+/", re.I),
    re.compile(r"`[^`]{3,}`"),                       # 反引号里的命令/片段
    re.compile(r"\b[\w-]+\.(py|js|ts|go|java|rb|sh)\b"),  # 具体文件
    re.compile(r"\btests?/[\w./-]+"),                 # 带路径段的测试路径(tests/xxx)
)


def _looks_like_verification_path(v: str) -> bool:
    """verification 里是否已给出具体可复现的东西(pytest 节点 id / 命令 / 具体文件 / 请求)。"""
    if any(sig in v for sig in _PATH_SIGNALS):
        return True
    return any(pat.search(v) for pat in _PATH_PATTERNS)
# 逃生口 "none" / "n/a" 只认短形式(文档约定为 "none: 属推演")
_MAX_NONE_FORM_LEN = 20
def _verification_is_speculative(issue: Any) -> bool:
    """LLM 是否在 verification 里自认"拿不出验证路径"(P0-1 的确定性止损口)。

    判定顺序(两条规则, 不再有"头部窗口"这种第三条):
      ① 已给出具体路径(节点 id / 命令 / 文件 / 请求) → 不算推演(路径优先)
      ② 短形式逃生口("none…" / "n/a…")              → 算推演
      ③ 文本里出现推演类标记词                        → 算推演

    演进说明(2026-09-13): R1 曾用"只看开头 24 字"来避免"路径 + 后半句补充说明"被误判。
    R2 引入路径优先判定后, 头部窗口变成多余且有害(会漏掉"无路径但把自认写在后面"的情况),
    故删除——现在只有"有没有给出具体路径"一个判据。
    """
    v = str(getattr(issue, "verification", "") or "").strip().lower()
    if not v:
        return False
    if _looks_like_verification_path(v):
        return False
    if v.startswith(("none", "n/a")) and len(v) <= _MAX_NONE_FORM_LEN:
        return True
    return any(m in v for m in _SPECULATION_MARKERS)


def _has_no_basis(issue: Any) -> bool:
    """门禁级问题既无可验证路径、也无判断依据 → 典型的严重度高判(报告分类 B)。"""
    v = str(getattr(issue, "verification", "") or "").strip()
    e = str(getattr(issue, "evidence", "") or "").strip()
    return not v and not e


# P0-5 规则(报告 §5): 机器已确认的维度(如 mypy 类型检查通过)上仍报问题 → 必须说明
# "为什么现有工具没拦住"。说不出理由 = 重复劳动或无依据断言, 降级到 2(不阻塞)。
# 依据: 15 轮实测里, "类型对不对/测试覆盖没覆盖"本属工具职责, 评审只能靠语言先验猜。
# 只收**缺口式**说法(评审 R1-2): 早先含裸 "检查范围" / "exclude", 会把"该问题在检查范围内,
# mypy 本应抓到"这类**相反语义**的表述也算成"已解释缺口" → 本该降级的门禁级问题被保留。
# 注意"漏"这类单字不收(会命中"漏洞"); 只收明确的否定/缺失短语。
def _mentions_tool_gap(issue: Any, tool: str = "") -> bool:
    """issue 是否显式说明了"为什么现有工具没拦住"(P0-5)。

    判据 = `tool_gap` 字段非空(**不看** detail/evidence 里的自由文本)。
    演进(三轮评审打穿同一条启发式):
      1. 曾用裸子串 "检查范围"      → "该问题在检查范围内, mypy 本应抓到"也命中(相反语义)
      2. 改成"文本里出现工具名就算"  → "mypy 本应抓到却没有"同样命中, 同族缺陷
      3. 补 "缺失/改名" 等否定词    → "参数类型标注缺失"这类无关文本又命中, 规则大面积空转
    结论: 从自由文本里猜"有没有解释"这条路走不通, 与 P0-1 一样改成**让模型显式填字段**;
    确定性层只判字段是否为空, 不再猜语义。参数 tool 仅供调用方拼理由文案。
    """
    return bool(str(getattr(issue, "tool_gap", "") or "").strip())


def _is_toolchain_covered_no_gap(issue: Any, verified_dimensions: dict[str, str]) -> bool:
    """该 issue 落在"机器已确认的维度"上, 且没说明工具为何没拦住。"""
    if not verified_dimensions:
        return False
    category = str(getattr(issue, "category", "") or "").strip().lower()
    dimension = CATEGORY_TOOL_DIMENSION.get(category)
    if not dimension or dimension not in verified_dimensions:
        return False
    return not _mentions_tool_gap(issue, verified_dimensions.get(dimension, ""))


def _high_judgement(issue: Any) -> tuple[int, str] | None:
    """门禁级严重度高判 → (目标档位, 命中的信号); 不属高判 → None。

    **分档唯一真源**(2026-09-13 评审 R1-2): 档位与"为什么降"都在这里定, 避免
    "是否算高判"与"降到几档"两处各写一套顺序而后漂移。

    优先级(自上而下, 前者命中即定档):
      1. verification 自认属推演(P0-1)        → 2: 连路径都拿不出, 映射表里"无路径"即轻微级
      2. 假设性措辞 / trigger=hypothetical   → 3: 假设性故障最高 3(必修不阻塞)
      3. 无 verification 且无 evidence        → 3: 无任何事实支点

    注: `category=convention` 由更早的 `_CONVENTION_TIER` 规则单独处理(建议档 2),
    不在这里——它跟 severity 是否 ≥4 无关。
    """
    if int(getattr(issue, "severity", 0) or 0) < 4:
        return None
    if _verification_is_speculative(issue):
        return (2, "speculative")
    if _looks_hypothetical(issue):
        return (3, "hypothetical_wording")
    if str(getattr(issue, "trigger", "") or "").strip().lower() == "hypothetical":
        return (3, "trigger_hypothetical")
    if _has_no_basis(issue):
        return (3, "no_basis")
    return None


# P0-5 职责边界(用户 2026-09-13 拍板"按 P0-5 来"): 纯约定违反(category=convention)
# 属"风格/约定"维度 → **建议档 2**, 不计门禁。原先的锚点"明确约定违反=3(必修不阻塞)"
# 已随本决定废止(动机: 约定类问题该由 ruff/仓库约定文档负责, 评审报它既重复又与门禁无关)。
_CONVENTION_TIER = 2

# 命中信号 → 降级理由(P0-1 文案的唯一来源)
_HIGH_JUDGEMENT_REASONS = {
    "speculative": "自认可验证路径属推演(P0-1 无可验证路径)",
    "hypothetical_wording": "证据/描述呈假设性故障",
    "trigger_hypothetical": "LLM 自标 trigger=hypothetical",
    "no_basis": "既无可验证路径也无判断依据",
}


def _is_severity_high_judgement(issue: Any) -> bool:
    """该 issue 是否属"严重度高判"(会拦合并且应降级)。判定见 _high_judgement。"""
    return _high_judgement(issue) is not None


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


def per_issue_verify(
    issues: list[Any],
    added_lines: dict[str, set[int]],
    verified_dimensions: dict[str, str] | None = None,
) -> list[IssueVerdict]:
    """确定性逐条验证(零成本, 不调 LLM)。

    规则(对应 docs/pr-review-quality-gate.md §4 与 review-severity-policy skill):
    1. 行号缺失(0 或 None) → delete(无法定位到 diff 行, 疑似幻觉)
    2. 行号不在 diff 新增行 → delete(疑似幻觉; 只评本 PR 引入的问题)
    3. severity 越界(非 1-5) → fix(钳制到合法范围)
    4. 假设性证据 / LLM 自标 trigger=hypothetical / 纯约定违反 /
       门禁级(≥4)但既无 verification 又无 evidence → downgrade 到 3(必修不阻塞)
    4a. 纯约定违反(category=convention)→ downgrade 到 2(建议档, 不计门禁; P0-5)
    4b. 自认可验证路径属推演(none: 属推演)+ 级别 ≥4 → downgrade 到 2(轻微)
       - 报告 P0-1 指定; 与 severity 映射表"无路径→轻微"一致
    4c. 落在"机器已确认的维度"上(如 CI mypy 通过)且未说明工具为何没拦住 → downgrade 到 2
       - P0-5 职责边界: 该维度由工具负责, 说不出"工具漏在哪"就是重复劳动/无依据断言
       - verified_dimensions: {维度: 工具名}; 为空(未声明/未确认)→ 本规则不生效

    返回 verdict 列表(与 issues 一一对应)。
    """
    verified = verified_dimensions or {}
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

        # 4a. 纯约定违反 → 建议档(P0-5: 风格/约定不计门禁)
        if (
            str(getattr(issue, "category", "") or "").strip().lower() == "convention"
            and sev > _CONVENTION_TIER
        ):
            verdicts.append(
                IssueVerdict(
                    issue=issue, action=ACTION_DOWNGRADE, new_severity=_CONVENTION_TIER,
                    reason=(
                        f"纯约定违反属建议档(P0-5 职责边界: 风格/约定不计门禁), "
                        f"从 {sev} 降级到 {_CONVENTION_TIER}"
                    ),
                )
            )
            continue

        # 4. 严重度高判(≥4, 会拦合并) → 降级到 3(必修不阻塞)
        #    信号: 假设性措辞 / trigger=hypothetical / 自认推演 / 无依据
        #    (约定违反已由 4a 单独处理成建议档)
        high = _high_judgement(issue)
        if high is not None:
            target, signal = high
            reason = (
                f"严重度高判(级别 {sev} ≥4 会拦合并): "
                f"{_HIGH_JUDGEMENT_REASONS.get(signal, signal)}, 降级到 {target}"
            )
            verdicts.append(
                IssueVerdict(issue=issue, action=ACTION_DOWNGRADE,
                             reason=reason, new_severity=target)
            )
            continue

        # 4c. 机器已确认的维度上仍报问题, 且没说明工具为何没拦住(P0-5)
        if _is_toolchain_covered_no_gap(issue, verified):
            dimension = CATEGORY_TOOL_DIMENSION.get(
                str(getattr(issue, "category", "") or "").strip().lower(), ""
            )
            tool = verified.get(dimension, "") or dimension
            verdicts.append(
                IssueVerdict(
                    issue=issue, action=ACTION_DOWNGRADE, new_severity=2,
                    reason=(
                        f"{dimension} 维度已由机器确认通过({tool}), 但未说明"
                        f"「为什么它没拦住」, 降级到 2(该维度应由工具回答)"
                    ),
                )
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
