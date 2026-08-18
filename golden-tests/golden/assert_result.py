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
    # 例 case-bug 期望 [4,5](数字分级 2026-08-18), 报出 4/5 + 若干 2 仍 pass(噪音不误杀)
    required_sev = expect.get("severities") or []
    if required_sev:
        reported = _reported_levels(actual)
        if not (reported & set(required_sev)):
            failures.append(
                f"未命中期望级别 {required_sev}(实际报出 {sorted(reported) or '无'})"
            )

    # 禁止报出的级别——边界样本语义:
    # 例 case-bait 禁止 [4,5], 报 2/3 可接受, 报 4/5 = 严重度误判
    forbid_sev = expect.get("forbid_severities") or []
    for sev in forbid_sev:
        if int(actual.get(str(sev), 0)) > 0:
            failures.append(f"报出被禁止的 {sev} 级问题(严重度误判)")

    # 必须命中的类别(至少报出期望集合中的某个类别)——正样本语义:
    # 例 case-security 期望 ["security"], 报出的 issue 类别中至少一个为 security
    # categories 为 None 表示无法解析类别(旧评论无锚点), 跳过校验
    required_cat = expect.get("categories") or []
    if required_cat and actual.get("categories") is not None:
        actual_cats = set(actual.get("categories") or [])
        if not (actual_cats & set(required_cat)):
            failures.append(
                f"未命中期望类别 {required_cat}(实际 {sorted(actual_cats) or '无'})"
            )

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


# 旧评论(回退正则)级别映射: error→4, warn→2, info→1
_LEGACY_SEV = {"error": 4, "warn": 2, "info": 1}


def _reported_levels(actual: dict[str, Any]) -> set[int]:
    """从 parse_comment_issues 结果提取报出的级别集合(1-5), 兼容旧 key。"""
    reported: set[int] = set()
    for sev, count in actual.items():
        if sev in ("1", "2", "3", "4", "5") and int(count) > 0:
            reported.add(int(sev))
        elif sev in _LEGACY_SEV and int(count) > 0:
            reported.add(_LEGACY_SEV[sev])
    return reported
