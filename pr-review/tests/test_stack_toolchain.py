"""P0-4/P0-5 单测: 技术栈/语义声明 + 工具链事实注入 + 职责边界确定性规则。

报告来源: docs/pr-review-false-positive-field-report.md §3.1 与 §5(P0-4 / P0-5)。
本文件覆盖三件事:
1. 配置能声明技术栈与工具链(.ai-review.yaml → StackConfig / ToolchainConfig)
2. 事实注入: 语义清单 + "已由机器确认"的维度, 且**只认 CI 真实结论**(拿不到就不注入)
3. 确定性规则: 机器已确认的维度上仍报问题却不说明工具缺口 → 降级(不阻塞)
"""

from unittest.mock import MagicMock

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
    assert "为什么现有工具没拦住它" in flat
    assert "convention" in flat


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


def test_p05_keeps_when_gap_explained_by_tool_name():
    """只提工具名(不含"未覆盖/未检查"等标记词)也必须算作已解释缺口。

    这条单独存在是有原因的: 另一条用例的文本同时命中了标记词, 若只测那一条,
    "点了工具名"这条路径就是空转的(变异测试曾抓到这一点)。
    """
    issue = _issue(verification="该类型问题由 mypy 负责, 本次该 check 在 CI 里缺失(仓库改名)")
    hay = " ".join([issue.detail, issue.evidence, issue.verification, issue.suggestion])
    assert not any(m in hay for m in ("未覆盖", "未检查", "noqa", "type: ignore")), "本用例必须只走工具名路径"
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == ACTION_KEEP


def test_p05_keeps_when_gap_explained_by_ignore_marker():
    issue = _issue(detail="该行有 type: ignore 掩盖了不一致")
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy"})[0]
    assert v.action == ACTION_KEEP


def test_p05_inactive_when_dimension_not_verified():
    """反向守卫: 该维度没被机器确认(如仓库没接 mypy) → 规则不生效, 不降级。"""
    v = per_issue_verify([_issue()], {"a.py": {1}}, verified_dimensions={"style": "ruff"})[0]
    assert v.action == ACTION_KEEP


def test_p05_inactive_when_no_toolchain_declared():
    """反向守卫: 没声明工具链(旧行为) → 完全不影响既有判定。"""
    v = per_issue_verify([_issue()], {"a.py": {1}})[0]
    assert v.action == ACTION_KEEP


def test_p05_does_not_downgrade_bug_category():
    """反向守卫: bug/security 类问题不属任何工具维度, 不能被这条规则降级。"""
    issue = _issue(category="bug", severity=5, detail="nil 解引用必然 panic")
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"typing": "mypy", "style": "ruff"})[0]
    assert v.action == ACTION_KEEP


def test_p05_convention_covered_by_style():
    issue = _issue(category="convention", severity=3, detail="命名不符合约定")
    v = per_issue_verify([issue], {"a.py": {1}}, verified_dimensions={"style": "ruff"})[0]
    assert v.action == ACTION_DOWNGRADE and v.new_severity == 2


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
