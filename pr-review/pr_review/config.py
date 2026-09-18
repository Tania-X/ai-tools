"""审查行为配置(.ai-review.yaml)。

与 gateway 配置(模型/key)分离:这里只控制"审什么、多细、发不发"。
参考 .coderabbit.yaml 的哲学:初版先 chill,控制噪音。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyYAML 是 pr-review 唯一新增运行时依赖
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时给出可读错误
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger("pr_review.config")

# 严重级别从高到低(旧字符串, 兼容迁移/测试)
SEVERITIES = ("error", "warn", "info")
# 旧字符串 → 1-5 数字(2026-08-18 数字分级: 1建议 2轻微 3必修 4严重 5致命)
LEGACY_SEVERITY_MAP = {"info": 1, "warn": 2, "error": 4}


def _parse_severity(value, default: int, *, allow_off: bool = False) -> int:
    """severity 配置解析: 支持 1-5 数字与旧字符串(error/warn/info/off)。"""
    if value is None:
        return default
    if isinstance(value, int):
        return value if 1 <= value <= 5 else default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in LEGACY_SEVERITY_MAP:
            return LEGACY_SEVERITY_MAP[v]
        if allow_off and v == "off":
            return 0
        try:
            n = int(v)
            return n if 1 <= n <= 5 else default
        except ValueError:
            return default
    return default


@dataclass
class QualityConfig:
    """质量门配置(LLM-as-judge 自检循环, P1b)。

    详设: docs/pr-review-quality-gate.md
    """

    # 一键开关:关闭则完全跳过 judge 打分与重写
    enabled: bool = True
    # judge 用模型(空=与审查同模型; 独立配置时传 model 覆盖)
    judge_model: str = ""
    # judge 用 provider(空=与审查同 provider; 独立配置时传 provider 覆盖, 模型路由生产化)
    judge_provider: str = ""
    # 低于该分触发重写; 太高重试爆炸, 太低失去门禁意义
    pass_score: int = 70
    # 重写硬上限, 防死循环; 耗尽后降级(说明评论 + check neutral)
    max_rewrites: int = 3
    # judge 输入预算(大 PR 截断用)
    max_judge_input_chars: int = 8000
    # judge 输出预算(token)。**不能小**: rubric 有 6 个维度, 而它被要求"逐条说明扣分点",
    # 中文一条理由约 200 字 ≈ 120-200 token, 3 条就吃掉 400。此前硬编码 400 的后果是
    # 线上 judge 连续两轮都在"第三条理由"中间被截断 → JSON 缺收尾 → 连续解析失败 → 评分不可用。
    # 参照 review_max_tokens(见下方注释: 连 gateway 默认的 1024 都被判定为"太小"): 给 judge 的
    # 输出预算不该比审查本体还紧。调大上限不增加成本——输出 token 只在真写出来时才计费。
    judge_max_tokens: int = 1200
    # linter 交叉验证层(首版仅预留, 未实现; 开启需工作流安装对应 linter)
    lint_enabled: bool = False
    lint_only: list[str] = field(default_factory=lambda: ["py", "go", "js"])


@dataclass
class ReviewToolsConfig:
    """agentic 审查工具(仓库代码访问, 形态二, 2026-08-15)。"""

    # 一键开关: 关闭则审查退化为纯 prompt(不注入工具)
    enabled: bool = True
    # 单轮审查最多工具调用次数(硬上限, 防 agent 无限探索)
    max_tool_calls: int = 12
    # 单次工具结果最多字符(防 token 爆炸)
    max_result_chars: int = 6000
    # 单文件最多读多少行(防大文件爆上下文)
    max_file_lines: int = 200


# ── P0-4/P0-5: 技术栈声明 + 工具链声明 ──────────────────────────────────────
# 动机(误报现场报告 §3.1/§5): 15 轮门禁级判定里, 8 条误报是"正确代码 + 与 Java/JS
# 直觉相反的语言规则"; 另有相当部分在问"类型对不对/测试覆盖没覆盖"——那是 mypy 与
# pytest 的职责。声明技术栈 + 注入工具链真实结果, 是让模型不去猜它没有事实来源的事。

# 语言语义清单(P0-4): 每条的左边是"别的语言的直觉", 右边是 Python 的实际规则。
# 这份清单不是泛泛的风格建议, 而是本仓 8 个实例逐条对应的确定性规则。
LANGUAGE_SEMANTICS: dict[str, list[str]] = {
    "python": [
        "真值判断看容器是否为空: `{\"data_sources\": []}` 是**非空 dict** → `if filters:` 为真; "
        "只有空容器/空串/None/0 才为假(JS 里 `[]` 为假, Python 不同)",
        "异常可按类集中注册处理器(框架级 `@app.exception_handler(X)`, 如 Starlette 沿 MRO 找最精确处理器): "
        "**不需要每个路由 try/catch**, 也不能据此判定'异常会变成 500'",
        "异步生命周期: `async with` 块内 `await` 完成后才执行 `__exit__`(块内 await 不等于资源已释放); "
        "**async generator 必须用 `async with` 包裹**, 直接 `await`/同步 with 会 TypeError",
        "普通 `@dataclass`(含 frozen=True)**不校验**参数值: 值对象的构造器不做范围检查, "
        "非法值要等到查库/比较时才体现(常表现为 404 而非 500)",
        "动态类型: **没有编译期类型检查**, 类型不一致会延迟到运行时才炸; 这类问题属 mypy/类型检查器的职责",
        "`x or default` 惯用法里 default 常来自配置对象(如 `settings.foo`), **不是硬编码常量**; "
        "同理 `field(default_factory=list)` 是可变默认值的**正确修法**, 不是缺陷",
    ],
}

# 分类 → 工具链维度(P0-5): 用于"这个问题本该由哪个机器负责"的映射
CATEGORY_TOOL_DIMENSION: dict[str, str] = {
    "type_consistency": "typing",
    "convention": "style",
    "style": "style",
}


@dataclass
class ToolchainCheck:
    """一条由机器负责的检查(声明式, 不含命令执行)。"""

    name: str = ""            # CI 里的 check 名(如 "Type check (uv run mypy)"), 用于匹配 check-run
    dimension: str = ""       # typing / style / test / build
    tool: str = ""            # 工具名(如 mypy / ruff / pytest), 注入 prompt 与"为何没拦住"判断用


@dataclass
class StackConfig:
    """P0-4: 技术栈声明(仓库声明优先, 缺失时可从 diff 后缀推断语言)。"""

    languages: list[str] = field(default_factory=list)   # python / javascript / go ...
    framework: str = ""
    async_runtime: str = ""
    notes: list[str] = field(default_factory=list)        # 仓库特有的语义提醒

    def semantics(self) -> list[str]:
        """按语言取语义清单(未声明的语言不影响输出)。"""
        out: list[str] = []
        for lang in self.languages:
            out.extend(LANGUAGE_SEMANTICS.get(lang.strip().lower(), []))
        return out


@dataclass
class ToolchainConfig:
    """P0-5: 工具链声明。checks 用于把 CI check-run 匹配到"哪个维度已被机器确认"。"""

    enabled: bool = False
    checks: list[ToolchainCheck] = field(default_factory=list)

    def dimension_for(self, category: str) -> str:
        return CATEGORY_TOOL_DIMENSION.get((category or "").strip().lower(), "")

    def tool_for_dimension(self, dimension: str) -> str:
        """该维度由哪个工具负责(首个声明的)。"""
        for c in self.checks:
            if c.dimension == dimension:
                return c.tool
        return ""


@dataclass
class ReviewConfig:
    # 审查重点(直接作为指令进入 prompt)
    review_focus: list[str] = field(
        default_factory=lambda: [
            "bug 与逻辑错误",
            "并发/性能隐患",
            "安全问题(注入/越权/密钥泄露)",
            "资源泄漏(连接/文件未关闭)",
            "明显不符合项目既有约定(命名/分层/异常处理)",
        ]
    )
    # 忽略的路径(glob, 支持 **)
    ignore_paths: list[str] = field(
        default_factory=lambda: [
            "**/*.lock",
            "**/package-lock.json",
            "**/pnpm-lock.yaml",
            "**/yarn.lock",
            "**/*.min.js",
            "**/*.min.css",
            "**/vendor/**",
            "**/generated/**",
            "**/dist/**",
            "**/build/**",
            "**/target/**",
            "**/.idea/**",
            "**/.vscode/**",
        ]
    )
    # 只发出达到该级别及以上的问题(1-5 数字, 1建议~5致命; 兼容旧字符串 error→4/warn→2/info→1)
    min_severity: int = 2
    # 合并门禁(阻塞线): 存在达到该级别及以上的问题时, check-run 失败 + job exit 1(PR 变红)
    #   4 = 严重及以上拦(推荐) | 3 = 必修也拦(激进) | 0 = 永不拦(只发评论)
    fail_on_severity: int = 4
    # 必修线: 达到该级别及以上的问题标记"必修"(2026-08-18 设计: 基线 2.5 语义 =
    #   只有有真实触发路径/约定违反的 ≥3 级强制修, 假设性 2 级仅提醒)
    require_fix_severity: int = 3
    # 切片:每批最多文件数(大 PR 分批审,控制单次 prompt token)
    max_files_per_batch: int = 20
    # 单文件 patch 超过该行数则截断(在评论中提示)
    max_lines_per_file: int = 800
    # 是否在评论中附带模型/token/成本统计
    show_stats: bool = True
    # 仓库上下文:审查前收集这些文件(glob,相对仓库根)注入 prompt,
    # 让 AI 结合项目约定/API 契约判断(第一轮评估 2/3 误报的根因是缺此上下文)
    context_files: list[str] = field(
        default_factory=lambda: [
            "AGENTS.md",
            "README.md",
            "docs/**/*.md",
            "spec/**",
        ]
    )
    # 上下文注入的总预算(字符),超预算按配置顺序截断
    max_context_chars: int = 8000
    # 生成代码检测:文件头含 Generated by/@generated/DO NOT EDIT 标记的跳过审查
    ignore_generated: bool = True
    # 线程决议驱动(P1a):用户在线程表态(resolve/ignore)后,下轮 review 不再重复报
    resolve_enabled: bool = True
    # 已处理清单注入 prompt 的上限条数(超限截断,防 prompt 膨胀)
    max_handled_lines: int = 200
    # 质量门(P1b):judge 打分 + 自检重写
    quality_gate: QualityConfig = field(default_factory=QualityConfig)
    # 审查调用的 LLM 温度: 审查是判断型任务, 低温度提升稳定性(避免同一 diff 时报时不报);
    # 回复模式(P0)仍用 gateway 默认温度(对话需要一点随机性)
    review_temperature: float = 0.3
    # 审查批次输出预算(token): gateway 默认 max_tokens=1024 太小,
    # 多 issues JSON 会被截断成非法 JSON → 静默空结果事故(2026-08-14 线上 bug)
    review_max_tokens: int = 4096
    # agentic 审查工具(仓库代码访问, 形态二)
    review_tools: ReviewToolsConfig = field(default_factory=ReviewToolsConfig)
    # 技术栈声明(P0-4): 语言/框架 + 语言语义清单注入
    stack: StackConfig = field(default_factory=StackConfig)
    # 工具链声明(P0-5): 哪些维度已由机器负责(ruff/mypy/pytest)
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)

    def severity_rank(self, severity: str) -> int:
        """兼容旧调用: 字符串级别 → 数字(2026-08-18 后主逻辑直接用 int)。"""
        return _parse_severity(severity, 2)

    def passes_filter(self, severity: int) -> bool:
        """是否达到 min_severity 门槛(数字比较, 1-5)。"""
        return severity >= self.min_severity

    def should_ignore(self, path: str) -> bool:
        from fnmatch import fnmatch

        p = path.replace("\\", "/")
        for pat in self.ignore_paths:
            if fnmatch(p, pat):
                return True
            # fnmatch 中 "**/" 需要路径含斜杠,补一次根路径匹配:
            # "**/package-lock.json" 也要命中仓库根的 package-lock.json
            if pat.startswith("**/") and fnmatch(p, pat[3:]):
                return True
        return False


DEFAULT_CONFIG = ReviewConfig()


def _warn_pyyaml_missing(path: str | Path) -> None:
    """PyYAML 缺失、配置却存在时报警。

    用 logger.warning（与 gateway/config.py 同款仓库惯例）。
    实测可达性：main.py 走 basicConfig 时是有格式的 WARNING 行；
    pr_review/cli.py 未配置 logging 时由 logging.lastResort 兜底输出到 stderr。
    两个入口都可见，因此不额外引入 warnings 模块。
    """
    logger.warning(
        "PyYAML 未安装，无法解析审查配置 %s —— 该文件将被忽略，"
        "review_focus / min_severity / fail_on_severity / context_files 等"
        "全部退回默认值。请安装依赖：pip install PyYAML",
        path,
    )


def load_config(path: str | Path | None = None) -> ReviewConfig:
    """加载 .ai-review.yaml;文件缺失/为空时使用默认配置。

    注意:返回全新实例,绝不修改模块级 DEFAULT_CONFIG 单例。
    """
    cfg = ReviewConfig()  # 默认值与 DEFAULT_CONFIG 一致,但独立可变
    if yaml is None:
        # 调用方明确给了配置文件、文件也确实存在，却因为缺依赖读不了：
        # 这是必须报出来的环境错误，不能静默回落（否则"配了但没生效"不留任何痕迹）。
        # 仍然返回默认配置，保持"配置缺失也能跑"的既有约定，不抛异常。
        if path is not None and Path(path).is_file():
            _warn_pyyaml_missing(path)
        return cfg
    if path is None or not Path(path).is_file():
        return cfg

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    focus = data.get("review_focus")
    if focus:
        cfg.review_focus = list(focus)
    ignore = data.get("ignore_paths")
    if ignore:
        cfg.ignore_paths = list(ignore)
    # severity 配置: 支持数字(1-5)与旧字符串(error/warn/info/off)兼容
    cfg.min_severity = _parse_severity(data.get("min_severity"), cfg.min_severity)
    cfg.fail_on_severity = _parse_severity(data.get("fail_on_severity"), cfg.fail_on_severity, allow_off=True)
    cfg.require_fix_severity = _parse_severity(
        data.get("require_fix_severity"), cfg.require_fix_severity
    )
    cfg.max_files_per_batch = int(data.get("max_files_per_batch", cfg.max_files_per_batch))
    cfg.max_lines_per_file = int(data.get("max_lines_per_file", cfg.max_lines_per_file))
    cfg.show_stats = bool(data.get("show_stats", cfg.show_stats))
    cfg.max_context_chars = int(data.get("max_context_chars", cfg.max_context_chars))
    if data.get("context_files"):
        cfg.context_files = list(data["context_files"])
    if "ignore_generated" in data:
        cfg.ignore_generated = bool(data["ignore_generated"])
    if "resolve_enabled" in data:
        cfg.resolve_enabled = bool(data["resolve_enabled"])
    cfg.max_handled_lines = int(data.get("max_handled_lines", cfg.max_handled_lines))
    if "review_temperature" in data:
        cfg.review_temperature = float(data["review_temperature"])
    if "review_max_tokens" in data:
        cfg.review_max_tokens = int(data["review_max_tokens"])
    # review_tools 块(agentic 仓库访问)
    rt = data.get("review_tools") or {}
    if isinstance(rt, dict):
        if "enabled" in rt:
            cfg.review_tools.enabled = bool(rt["enabled"])
        if "max_tool_calls" in rt:
            cfg.review_tools.max_tool_calls = int(rt["max_tool_calls"])
        if "max_result_chars" in rt:
            cfg.review_tools.max_result_chars = int(rt["max_result_chars"])
        if "max_file_lines" in rt:
            cfg.review_tools.max_file_lines = int(rt["max_file_lines"])
    # stack 块(P0-4): 技术栈声明
    st = data.get("stack") or {}
    if isinstance(st, dict):
        if st.get("languages"):
            cfg.stack.languages = [str(x) for x in st["languages"]]
        if st.get("framework"):
            cfg.stack.framework = str(st["framework"])
        if st.get("async_runtime"):
            cfg.stack.async_runtime = str(st["async_runtime"])
        if isinstance(st.get("notes"), list):
            cfg.stack.notes = [str(x) for x in st["notes"]]
    # toolchain 块(P0-5): 工具链声明
    tc = data.get("toolchain") or {}
    if isinstance(tc, dict):
        if "enabled" in tc:
            cfg.toolchain.enabled = bool(tc["enabled"])
        checks: list[ToolchainCheck] = []
        for item in tc.get("checks") or []:
            if not isinstance(item, dict):
                continue
            checks.append(ToolchainCheck(
                name=str(item.get("name", "")),
                dimension=str(item.get("dimension", "")),
                tool=str(item.get("tool", "")),
            ))
        if checks:
            cfg.toolchain.checks = checks

    # quality_gate 块(缺失则用默认; lint 层首版仅预留)
    qg = data.get("quality_gate") or {}
    if isinstance(qg, dict):
        if "enabled" in qg:
            cfg.quality_gate.enabled = bool(qg["enabled"])
        if qg.get("judge_model"):
            cfg.quality_gate.judge_model = str(qg["judge_model"])
        # judge 可独立 provider（如便宜模型 / 消除同源自评偏差）。
        # quality.py 会读取该字段路由 judge 调用，但此前加载器漏了这一步，
        # 导致文档承诺的 judge_provider 被静默忽略 —— 由 test_load_config_quality_gate 守护。
        if qg.get("judge_provider"):
            cfg.quality_gate.judge_provider = str(qg["judge_provider"])
        cfg.quality_gate.pass_score = int(qg.get("pass_score", cfg.quality_gate.pass_score))
        cfg.quality_gate.max_rewrites = int(qg.get("max_rewrites", cfg.quality_gate.max_rewrites))
        cfg.quality_gate.max_judge_input_chars = int(
            qg.get("max_judge_input_chars", cfg.quality_gate.max_judge_input_chars)
        )
        # judge 输出预算: 与本文件 review_max_tokens 同性质的可调项。
        # 必须在这里显式加载 —— 只改 QualityConfig 默认值而不读 YAML, 会重演
        # judge_provider 那次"配置项被静默忽略"的事故(由测试守护)。
        cfg.quality_gate.judge_max_tokens = int(
            qg.get("judge_max_tokens", cfg.quality_gate.judge_max_tokens)
        )
        if "lint_enabled" in qg:
            cfg.quality_gate.lint_enabled = bool(qg["lint_enabled"])
        if qg.get("lint_only"):
            cfg.quality_gate.lint_only = list(qg["lint_only"])
    return cfg
