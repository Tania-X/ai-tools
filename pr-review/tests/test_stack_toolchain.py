"""P0-4/P0-5 单测: 技术栈/语义声明 + 工具链事实注入 + 职责边界确定性规则。

报告来源: docs/pr-review-false-positive-field-report.md §3.1 与 §5(P0-4 / P0-5)。
本文件覆盖三件事:
1. 配置能声明技术栈与工具链(.ai-review.yaml → StackConfig / ToolchainConfig)
2. 事实注入: 语义清单 + "已由机器确认"的维度, 且**只认 CI 真实结论**(拿不到就不注入)
3. 确定性规则: 机器已确认的维度上仍报问题却不说明工具缺口 → 降级(不阻塞)
"""

from unittest.mock import MagicMock, patch

import pytest

from pr_review.config import (
    CATEGORY_TOOL_DIMENSION,
    ReviewConfig,
    StackConfig,
    ToolchainCheck,
    ToolchainConfig,
    load_config,
)
from pr_review.prompt import (
    SYSTEM_PROMPT,
    build_messages,
    build_stack_block,
)
from pr_review.quality import (
    ACTION_DOWNGRADE,
    ACTION_KEEP,
    per_issue_verify,
    structural_signals,
)
from pr_review.models import PRInfo
from pr_review.review import ReviewIssue, ReviewRunner


# --------------------------------------------------------------- 配置层
def test_load_stack_and_toolchain_from_yaml(tmp_path):
    cfg_file = tmp_path / ".ai-review.yaml"
    cfg_file.write_text(
        """
stack:
  languages: [python]
  framework: fastapi
  async_runtime: anyio
  notes:
    - 领域错误由 errors.py 集中注册 handler
toolchain:
  enabled: true
  checks:
    - {name: "Lint (ruff)", dimension: style, tool: ruff}
    - {name: "Type check (mypy)", dimension: typing, tool: mypy}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.stack.languages == ["python"]
    assert cfg.stack.framework == "fastapi"
    assert cfg.stack.notes and "集中注册" in cfg.stack.notes[0]
    assert cfg.toolchain.enabled is True
    assert [c.dimension for c in cfg.toolchain.checks] == ["style", "typing"]
    assert cfg.toolchain.tool_for_dimension("typing") == "mypy"


def test_missing_sections_keep_defaults_and_no_toolchain(tmp_path):
    """反向守卫: 没声明 stack/toolchain 时不能凭空造出"已确认"事实。"""
    cfg_file = tmp_path / ".ai-review.yaml"
    cfg_file.write_text("min_severity: 2\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.stack.languages == []
    assert cfg.toolchain.enabled is False
    assert cfg.toolchain.checks == []
    assert cfg.stack.semantics() == []


def test_stack_semantics_only_for_declared_language():
    """语义清单按语言给: 未声明的语言不出清单(避免给 Go 仓库塞 Python 规则)。"""
    assert StackConfig(languages=["python"]).semantics()
    assert StackConfig(languages=["go"]).semantics() == []
    assert StackConfig(languages=[]).semantics() == []


def test_python_semantics_cover_report_cases():
    """P0-4 的清单必须覆盖报告 §3.1 的实例(真值判断/异常映射/异步/数据类/动态类型/配置默认)。"""
    text = "\n".join(StackConfig(languages=["python"]).semantics())
    for keyword in ["真值判断", "异常", "async", "dataclass", "动态类型", "default_factory"]:
        assert keyword.lower() in text.lower(), f"语义清单缺 {keyword}"


def test_category_dimension_mapping_is_the_p0_5_bridge():
    assert CATEGORY_TOOL_DIMENSION["type_consistency"] == "typing"
    assert CATEGORY_TOOL_DIMENSION["convention"] == "style"


# --------------------------------------------------------------- 事实注入
def test_stack_block_contains_languages_semantics_and_verified():
    stack = StackConfig(languages=["python"], framework="fastapi", notes=["注意 A"])
    block = build_stack_block(stack, [{"dimension": "typing", "tool": "mypy", "name": "Type check"}])
    assert "技术栈与语言语义边界" in block
    assert "python" in block and "fastapi" in block
    assert "注意 A" in block
    assert "真值判断" in block          # 语义清单
    assert "typing" in block and "mypy" in block  # 机器已确认维度
    assert "为什么现有工具没拦住" in block


def test_stack_block_empty_when_nothing_declared():
    """反向守卫: 什么都没声明时不注入空块(否则每轮 prompt 多一段噪音)。"""
    assert build_stack_block(StackConfig(), []) == ""


def test_stack_block_without_verified_says_nothing_about_machine():
    """反向守卫: 没有 CI 事实时不能声称"已由机器确认"(那是编造事实)。"""
    block = build_stack_block(StackConfig(languages=["python"]), [])
    assert "已由机器确认" not in block


def test_build_messages_injects_stack_block_outside_system_prompt():
    pr = PRInfo(number=1, title="t", body="", head_sha="sha", head_ref="f", base_ref="main")
    block = build_stack_block(StackConfig(languages=["python"]), [])
    msgs = build_messages(pr, [], ReviewConfig(), stack_block=block)
    system, user = msgs[0]["content"], msgs[1]["content"]
    heading = "## 技术栈与语言语义边界(P0-4/P0-5)"
    assert heading in user
    assert heading not in system  # 动态事实不进 system 常量(规则里只是提到这个块的名字)
    assert "语言: python" in user


def test_prompt_has_semantics_and_toolchain_rules():
    # prompt 里规则会折行, 比较前先去掉空白, 避免测试被换行位置绑死
    flat = "".join(SYSTEM_PROMPT.split())
    assert "语言语义不得凭直觉断言" in flat
    assert "机器已确认的维度不要重复报" in flat
    assert "convention" in flat

    # 规则 20 与 JSON schema **各要出现一次 tool_gap**: 只断言"整份 prompt 里有"会被另一处掩盖
    # (变异测试抓到过: 改掉规则 20 里的字段名, 断言仍通过)
    rule_20 = flat[flat.index("20.【机器已确认的维度"):flat.index("输出JSON结构")]
    # 钉住"要求用语"而不只是字段名: 规则 20 里字段名出现两次, 只查字面量会被第二次掩盖
    assert "必须填写`tool_gap`字段" in rule_20, "规则 20 必须明确要求填 tool_gap"
    assert "为什么没拦住它" in rule_20
    schema = flat[flat.index("输出JSON结构"):]
    assert '"tool_gap"' in schema, "JSON schema 必须给出 tool_gap 字段"


# --------------------------------------------------------------- CI 事实采集
def _runner(platform, config=None):
    llm = MagicMock()
    llm.config.get.return_value = MagicMock(model="m")
    return ReviewRunner(platform=platform, llm=llm, config=config or ReviewConfig())


def _tc_cfg(**kw):
    cfg = ReviewConfig()
    cfg.toolchain = ToolchainConfig(enabled=True, checks=[
        ToolchainCheck(name="Type check (mypy)", dimension="typing", tool="mypy"),
        ToolchainCheck(name="Lint (ruff)", dimension="style", tool="ruff"),
    ])
    return cfg


def _pr():
    return PRInfo(number=1, title="t", body="", head_sha="abc", head_ref="f", base_ref="main")


def test_verified_dimensions_only_for_successful_checks():
    """只有结论为成功的 check 才算"机器已确认"; 失败/进行中不算。"""
    platform = MagicMock()
    platform.get_check_runs.return_value = [
        {"name": "Type check (mypy)", "conclusion": "success", "status": "completed"},
        {"name": "Lint (ruff)", "conclusion": "failure", "status": "completed"},
    ]
    runner = _runner(platform, _tc_cfg())
    assert runner._verified_dimension_tools(_pr()) == {"typing": "mypy"}


def test_verified_dimensions_skip_when_check_absent():
    """声明的 check 在 CI 里不存在(如改了名字) → 不确认该维度。"""
    platform = MagicMock()
    platform.get_check_runs.return_value = []
    runner = _runner(platform, _tc_cfg())
    assert runner._verified_dimension_tools(_pr()) == {}


def test_verified_dimensions_disabled_toolchain_does_not_call_api():
    platform = MagicMock()
    runner = _runner(platform, ReviewConfig())  # toolchain 默认 disabled
    assert runner._verified_dimension_tools(_pr()) == {}
    platform.get_check_runs.assert_not_called()


def test_verified_dimensions_tolerates_api_failure():
    """CI API 失败 → 视为未知(不注入), 且不把整轮审查搞挂。"""
    platform = MagicMock()
    platform.get_check_runs.side_effect = RuntimeError("403")
    runner = _runner(platform, _tc_cfg())
    assert runner._verified_dimension_tools(_pr()) == {}


def test_local_platform_has_no_ci_facts():
    from pr_review.local_platform import LocalPlatform

    assert LocalPlatform(".").get_check_runs("abc") == []


# --------------------------------------------------------------- 确定性规则(P0-5)
def _issue(category="type_consistency", severity=4, **kw):
    return ReviewIssue(
        file="a.py", line=1, severity=severity, title="t",
        detail=kw.pop("detail", "这里类型不一致"), suggestion="s",
        category=category, evidence=kw.pop("evidence", "a.py:1 标注 str 却传 int"),
        verification=kw.pop("verification", ""), **kw,
    )


def test_p05_downgrades_covered_dimension_without_gap_explanation():
    """机器已确认 typing 通过, 仍报类型问题且不说工具为何漏 → 降级到 2。"""
    v = per_issue_verify([_issue()], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == ACTION_DOWNGRADE
    assert v.new_severity == 2
    assert "mypy" in v.reason


def test_p05_keeps_when_tool_gap_field_filled():
    """P0-5 判据 = `tool_gap` 字段非空; 文案怎么写不影响判定。"""
    issue = _issue(category="type_consistency", severity=4, detail="类型不一致")
    issue.tool_gap = "该文件不在 mypy files 检查范围内(新目录未纳入)"
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == ACTION_KEEP


def test_p05_free_text_explanation_does_not_count():
    """评审 R4-1 的根因: 从自由文本猜"有没有解释"走不通——只认字段。

    修复前的关键词表包含 "缺失"/"改名", 于是"参数类型标注缺失"这类与工具缺口无关的描述
    也被算成解释 → 规则 4c 大面积空转。现在 detail 里写什么都不影响判定。
    """
    for text in (
        "参数类型标注缺失",
        "命名不规范, 建议改名",
        "mypy 本应抓到却没有",              # 反面: 想表达工具漏了, 也得写进 tool_gap
        "该文件不在检查范围",                # 连"正确的解释"写在 detail 里也不算
    ):
        issue = _issue(category="type_consistency", severity=4, detail=text)
        issue.verification = "见 a.py:12"
        v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
        assert v.action == ACTION_DOWNGRADE, f"{text!r} 写在 detail 里不算解释, 应降级"
        assert v.new_severity == 2


def test_p05_empty_or_blank_tool_gap_is_downgraded():
    """反向守卫: tool_gap 只有空白字符 = 没填。"""
    issue = _issue(category="type_consistency", severity=4)
    issue.tool_gap = "   \n  "
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == ACTION_DOWNGRADE


def test_p05_tool_gap_parsed_from_model_output():
    """字段要从模型输出解析出来(否则模型写了也没用)。"""
    issue = ReviewIssue.from_dict({
        "file": "a.py", "line": 1, "severity": "fatal", "title": "t", "detail": "d",
        "suggestion": "", "category": "type_consistency", "trigger": "real", "impact": "fatal",
        "tool_gap": "该 check 未配置",
    })
    assert issue.tool_gap == "该 check 未配置"


def test_p05_inactive_when_dimension_not_verified():
    """反向守卫: 该维度没被机器确认(如仓库没接 mypy) → 规则不生效, 不降级。"""
    issue = _issue(category="type_consistency", severity=4)
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"style": "ruff"})[0]
    assert v.action == ACTION_KEEP


def test_p05_inactive_when_no_toolchain_declared():
    """反向守卫: 没声明工具链(旧行为) → 完全不影响既有判定, 也不因 tool_gap 为空降级。"""
    v = per_issue_verify([_issue(category="type_consistency", severity=4)], {"a.py": {1}})[0]
    assert v.action == ACTION_KEEP


def test_p05_does_not_downgrade_bug_category():
    """反向守卫: bug/security 类问题不属任何工具维度, 不能被这条规则降级。"""
    issue = _issue(category="bug", severity=5, detail="nil 解引用必然 panic")
    v = per_issue_verify(
        [issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy", "style": "ruff"}
    )[0]
    assert v.action == ACTION_KEEP


def test_p05_convention_follows_advisory_rule_not_toolchain():
    """convention 现在由 4a(建议档)处理, 与工具链规则无关: 无论 tool_gap 是否填写都是 2。"""
    for gap in ("", "该规则 ruff 未覆盖"):
        issue = _issue(category="convention", severity=3, detail="命名不符合约定")
        issue.tool_gap = gap
        v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"style": "ruff"})[0]
        assert v.action == ACTION_DOWNGRADE and v.new_severity == 2
        assert "建议档" in v.reason


def test_p05_structural_signal_for_covered_dimension():
    signals = structural_signals([_issue()], {"a.py": {1}}, {"typing": "mypy"})
    assert any("机器已确认" in s and "typing" in s for s in signals)


def test_p05_no_signal_without_verified_dimensions():
    """反向守卫: 没有已确认维度时不得出 P0-5 信号(否则又是噪音)。"""
    signals = structural_signals([_issue()], {"a.py": {1}}, None)
    assert not any("机器已确认" in s for s in signals)


def test_p05_rule_runs_before_keep_but_after_hallucination_rules():
    """行号幻觉仍优先删除: 机器已确认也不该保留一条定位不到的问题。"""
    issue = _issue()
    issue.line = 999  # 不在新增行
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == "delete"


@pytest.mark.parametrize("category,dimension", list(CATEGORY_TOOL_DIMENSION.items()))
def test_p05_mapping_entries_are_actionable(category, dimension):
    """映射表里每一项都必须能配上声明的工具, 否则规则永远不生效(空转)。"""
    cfg = _tc_cfg()
    assert cfg.toolchain.tool_for_dimension(dimension), f"{category}→{dimension} 无对应工具"


# --------------------------------------------------------------- P0-5 事实的跨轮传播(评审 R1-1)
def _p05_runner(issues: list[dict], rounds: int = 2, check_conclusion: str = "success"):
    """构造带工具链声明的 runner: 平台返回成功的 check, LLM 产出指定 issues。"""
    import json as _json

    from gateway import ChatResponse

    issues_json = _json.dumps({"summary": "s", "issues": issues}, ensure_ascii=False)
    llm = MagicMock()
    llm.config.get.return_value = MagicMock(model="m")
    llm.chat.side_effect = [
        ChatResponse(content=issues_json, model="m", provider="p", usage={})
        for _ in range(rounds)
    ]
    platform = MagicMock()
    platform.get_pr_info.return_value = PRInfo(
        number=1, title="t", body="", head_sha="abc", head_ref="f", base_ref="main"
    )
    platform.get_pr_files.return_value = [{
        "filename": "src/a.py", "status": "modified",
        "patch": "@@ -1,1 +1,2 @@\n+def foo():\n+    pass",
    }]
    platform.get_check_runs.return_value = [
        {"name": "Type check (mypy)", "conclusion": check_conclusion, "status": "completed"}
    ]
    # 注意: 必须把上面这个带了 side_effect 的 llm 交给 runner — 早先这里误用了 _runner()
    # 另建的裸 MagicMock, 于是拿不到 JSON、进入工具循环, 测试假失败(自己踩过的坑)
    return ReviewRunner(platform=platform, llm=llm, config=_tc_cfg()), platform


def test_p05_verified_dimensions_survive_judge_rewrite_round():
    """评审 R1-1 回归: 重写轮必须继承"机器已确认维度", 否则 judge 的 P0-5 信号在重写轮消失。

    (评审把影响面写成"规则 4c 也失效"是不准的——4c 读的是 review 里的缓存;
    真正丢的是喂给 judge 的 structural_signals, 这条用例锁的就是那个通道。)
    """
    from pr_review.quality import JudgeResult

    runner, _ = _p05_runner([{
        "file": "src/a.py", "line": 1, "severity": "warn", "title": "A",
        "detail": "", "suggestion": "", "category": "bug",
    }])

    seen: list[dict[str, str]] = []

    def fake_eval(result, diff_text):
        seen.append(dict(getattr(result, "verified_dimensions", {}) or {}))
        if len(seen) == 1:
            return JudgeResult(score=50, verdict="rewrite", reasons=["噪音"])
        return JudgeResult(score=85, verdict="pass", reasons=[])

    with patch("pr_review.quality.Judge.evaluate", side_effect=fake_eval):
        result = runner.run()

    assert len(seen) == 2, f"应发生一次重写(judge 两轮), 实际 {len(seen)}"
    assert seen[0] == {"typing": "mypy"}
    assert seen[1] == {"typing": "mypy"}, "重写轮丢失了机器事实"
    assert result.verified_dimensions == {"typing": "mypy"}   # 最终结果也带着


def test_p05_verified_dimensions_survive_sentinel_rewrite():
    """评审 R1-1 回归(哨兵路径): 逐条验证触发整批重写时, 新 result 也要继承事实。"""
    runner, _ = _p05_runner([{
        "file": "src/a.py", "line": 1, "severity": "warn", "title": "类型不一致",
        "detail": "标注 str 却传 int", "suggestion": "",
        "category": "type_consistency", "evidence": "a.py:1 标注 str",
    }])
    # judge 用 patch 顶掉: 哨兵重写后是否再过 judge 取决于 fix/sentinel-round-judge 是否已合并,
    # 不 patch 的话两条分支合并后这次调用会多消耗一次 mock 响应 → StopIteration(纯夹具问题)。
    # 本用例只关心"事实跨轮存在", 故对两种顺序都成立。
    from pr_review.quality import JudgeResult

    with patch("pr_review.quality.Judge.evaluate",
               side_effect=lambda r, d: JudgeResult(score=80, verdict="pass", reasons=[])):
        result = runner.run()

    # type_consistency 落在已确认的 typing 维度且未说明工具缺口 → 降级 → 1/1 触发哨兵
    assert result.rewrites == 1, "哨兵应触发一次整批重写"
    assert result.verified_dimensions == {"typing": "mypy"}, "哨兵重写轮丢失机器事实"


def test_p05_rule_uses_single_source_of_truth():
    """回归守卫: 质量门不得绕开 result 自己重算事实(否则"重写轮丢事实"只在一侧暴露)。"""
    import inspect

    from pr_review import review as review_mod

    src = inspect.getsource(review_mod.ReviewRunner._quality_loop_inner)
    assert "verified_dimensions=result.verified_dimensions" in src
    assert "_verified_dimension_tools(pr)" not in src


# --------------------------------------------------------------- 评审 R1-2 / R1-3 回归
def test_r1_2_positive_phrasing_is_not_a_gap_explanation():
    """R1-2 回归(机制已演进): 自由文本里的这类表述不能算解释, 只有 tool_gap 字段算。

    历史: 最早 `_GAP_MARKERS` 含裸 "检查范围", 而它是 "在检查范围内" 的子串 → 误判为已解释
    → 规则 4c 失效。后续两轮又分别打穿了工具名路径与 "缺失/改名" 词表, 最终改为字段判据;
    本条用例钉住的是"自由文本说了什么都不算"这一契约。
    """
    issue = _issue(
        detail="该问题在检查范围内, mypy 本应抓到却没有",
        verification="见 a.py:12 的类型标注", category="type_consistency", severity=4,
    )
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 2



def test_r1_3_check_runs_pagination_collects_all_pages():
    """R1-3 回归: check-run 超过一页时不能漏(漏掉 → 该维度被当成"未知"而不注入)。"""
    from pr_review.github import GitHubClient

    client = GitHubClient(token="t", repo="o/r", pr_number=1)
    page1 = [{"name": f"check-{i}", "conclusion": "success", "status": "completed"} for i in range(100)]
    page2 = [{"name": "Type check (mypy)", "conclusion": "success", "status": "completed"}]
    calls: list[dict] = []

    def fake_get(path, params=None):
        calls.append(params or {})
        return {"check_runs": page1 if (params or {}).get("page") == 1 else page2}

    with patch.object(client, "_get", side_effect=fake_get):
        runs = client.get_check_runs("abc")
    assert len(runs) == 101
    assert runs[-1]["name"] == "Type check (mypy)"     # 第二页里的声明项没有丢
    assert [c.get("page") for c in calls] == [1, 2]     # 拉了两页就停


def test_r1_3_check_runs_single_page_no_extra_request():
    """R1-3 反向守卫: 不足一页时只请求一次(别为凑页数多打 API)。"""
    from pr_review.github import GitHubClient

    client = GitHubClient(token="t", repo="o/r", pr_number=1)
    calls: list[dict] = []
    with patch.object(client, "_get", side_effect=lambda path, params=None: (
        calls.append(params or {}) or {"check_runs": [{"name": "ci", "conclusion": "success"}]}
    )):
        runs = client.get_check_runs("abc")
    assert len(runs) == 1 and [c.get("page") for c in calls] == [1]


def test_r4_2_in_progress_check_is_not_verified():
    """R4-2 回归: 进行中的 check(只有 status, 没有 conclusion)不能算"机器已确认"。

    修复前 `conclusion or status` 会把 "in_progress" 塞进 conclusion —— 恰好不在白名单里所以
    侥幸没事, 但契约已被污染; 现在显式要求 status == completed, 并单独断言字段语义。
    """
    platform = MagicMock()
    platform.get_check_runs.return_value = [
        {"name": "Type check (mypy)", "conclusion": "", "status": "in_progress"},
    ]
    runner = _runner(platform, _tc_cfg())
    assert runner._verified_dimension_tools(_pr()) == {}


def test_r4_2_check_runs_keeps_conclusion_and_status_separate():
    """R4-2 回归: get_check_runs 返回的 conclusion 只放结论, status 单独保留。"""
    from pr_review.github import GitHubClient

    client = GitHubClient(token="t", repo="o/r", pr_number=1)
    with patch.object(client, "_get", side_effect=lambda path, params=None: {
        "check_runs": [{"name": "ci", "status": "in_progress", "conclusion": None}]
    }):
        runs = client.get_check_runs("abc")
    assert runs == [{"name": "ci", "conclusion": "", "status": "in_progress"}]
