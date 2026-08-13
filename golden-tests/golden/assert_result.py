"""断言期望(expected.json) vs 实际(审查结果), 判定单个 case 的 pass/fail(纯函数)。"""

from __future__ import annotations

from typing import Any


def evaluate(
    case_name: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    check_conclusion: str | None,
) -> dict[str, Any]:
    """判定单个 case 是否通过, 返回结构化结果。

    actual: parse_comment_issues 的输出(error/warn/info/total/no_issues)
    check_conclusion: "AI Review" check-run 的 conclusion(failure/success/neutral)
    """
    expect = expected.get("expect", {})
    failures: list[str] = []

    # 数量范围
    min_issues = expect.get("min_issues", 0)
    max_issues = expect.get("max_issues")
    total = int(actual.get("total", 0))
    if total < int(min_issues):
        failures.append(f"issues 数 {total} < min_issues {min_issues}")
    if max_issues is not None and total > int(max_issues):
        failures.append(f"issues 数 {total} > max_issues {max_issues}")

    # 必须命中的级别(至少报出期望集合中的某个级别)——正样本语义:
    # 例 case-bug 期望 ["error"], 报出 error + 若干 warn 仍 pass(噪音不误杀)
    required_sev = expect.get("severities") or []
    if required_sev:
        reported = {sev for sev in ("error", "warn", "info") if int(actual.get(sev, 0)) > 0}
        if not (reported & set(required_sev)):
            failures.append(
                f"未命中期望级别 {required_sev}(实际报出 {sorted(reported) or '无'})"
            )

    # 禁止报出的级别——边界样本语义:
    # 例 case-bait 禁止 ["error"], 报 warn/info 可接受, 报 error = 严重度误判
    forbid_sev = expect.get("forbid_severities") or []
    for sev in forbid_sev:
        if int(actual.get(sev, 0)) > 0:
            failures.append(f"报出被禁止的 {sev} 级问题(严重度误判)")

    # 质量门降级检测: 期望 pass 却 check neutral(质量门降级)
    if expect.get("quality_pass") and check_conclusion == "neutral":
        failures.append("质量门降级(check neutral), 但期望 pass")

    return {
        "case": case_name,
        "pass": not failures,
        "failures": failures,
        "actual": actual,
        "check_conclusion": check_conclusion,
    }
