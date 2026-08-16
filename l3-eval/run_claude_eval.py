"""L3 双引擎对比: 9 个 golden 场景批量评测 Claude Code + repo-tools MCP。

每个场景:
1. 基线代码复制到 ai-tools/l3-eval/<case>/src/(仓库上下文, Claude Code 可用工具查)
2. 生成 diff(基线 → 场景变更)
3. 调 claude -p 审查, 结果存 l3-eval/results/<case>.txt

运行: python l3-eval/run_claude_eval.py [场景名...]
环境: 需 ANTHROPIC_* + NODE_TLS_REJECT_UNAUTHORIZED=0 + claude 已配置 repo-tools MCP
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path

AI_TOOLS = Path(__file__).resolve().parent.parent
GOLDEN = Path.home() / "WorkBuddy" / "ai-review-golden-tests"
EVAL_DIR = AI_TOOLS / "l3-eval"
RESULTS_DIR = EVAL_DIR / "results"

# 场景: (case, 基线文件列表(src/), 变更文件列表(相对 golden scenarios/<case>/changes))
SCENARIOS: dict[str, dict] = {
    "case-bug": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "必然触发的 nil 解引用 bug",
    },
    "case-security": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "安全类问题",
    },
    "case-convention": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "约定违反",
    },
    "case-clean": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "干净代码, 应无问题",
    },
    "case-docs": {
        "base": [],
        "change": "README.md",
        "note": "纯文档变更, 应无问题",
    },
    "case-bait": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "性能/风格类, 不应报 error",
    },
    "case-merge-locations": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "同根因多位置, 应合并",
    },
    "case-severity-security": {
        "base": ["src/main.go"],
        "change": "src/main.go",
        "note": "错误忽略+安全后果, 应 warn 不 error",
    },
    "case-refactor-context": {
        "base": ["src/display.go"],
        "change": "src/user.go",
        "note": "跨文件行为变更, 需主动查代码",
    },
}

PROMPT_TEMPLATE = """你在做代码审查。以下是一个 PR 的 diff(变更内容):

```
{diff}
```

请审查这个 diff 是否引入问题。注意:变更可能影响仓库里**未变更的既有代码**(基线版本在 l3-eval/{case}/src/ 下),请使用 repo-tools MCP 工具(read_file / grep / ast_grep / list_dir)查看相关代码,确认影响。

输出审查结论(用中文):
- 按问题逐条列出:位置(文件:行号)、严重度(error/warn/info)、问题描述、修复建议
- 如果未发现问题,明确说"未发现问题"并简述排查过程
"""


def make_diff(base_text: str, change_text: str, from_path: str, to_path: str) -> str:
    diff = difflib.unified_diff(
        base_text.splitlines(keepends=True),
        change_text.splitlines(keepends=True),
        fromfile=f"a/{from_path}",
        tofile=f"b/{to_path}",
    )
    return "".join(diff)


def setup_scenario(case: str, spec: dict) -> tuple[str, str]:
    """复制基线到 l3-eval/<case>/src/, 生成 diff 文本。返回 (prompt_diff, note)。"""
    scene_dir = EVAL_DIR / case
    src_dir = scene_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # 复制基线文件
    for rel in spec["base"]:
        src = GOLDEN / rel
        if src.exists():
            shutil.copy2(src, src_dir / Path(rel).name)

    # 生成 diff
    change_path = GOLDEN / "scenarios" / case / "changes" / spec["change"]
    rel_change = spec["change"]
    base_path = GOLDEN / rel_change if rel_change.startswith("src/") else None
    if base_path and base_path.exists():
        diff = make_diff(
            base_path.read_text(encoding="utf-8"),
            change_path.read_text(encoding="utf-8"),
            rel_change,
            rel_change,
        )
    else:
        # 纯新增文件
        diff = (
            f"diff --git a/{rel_change} b/{rel_change}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n"
            f"+++ b/{rel_change}\n"
            + "".join(f"+{line}" for line in change_path.read_text(encoding="utf-8").splitlines(keepends=True))
        )
    return diff, spec["note"]


def run_claude(prompt: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_MODEL": "deepseek-v4-flash",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            # ANTHROPIC_API_KEY 从 ~/.zshrc 或环境继承; 若缺则从 shell 配置读
        }
    )
    if "ANTHROPIC_API_KEY" not in env:
        env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
    proc = subprocess.run(
        ["claude", "-p"],
        input=prompt,
        capture_output=True,
        text=True,
        cwd=AI_TOOLS,
        env=env,
        timeout=420,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def main() -> None:
    cases = sys.argv[1:] or list(SCENARIOS)
    RESULTS_DIR.mkdir(exist_ok=True)
    for case in cases:
        spec = SCENARIOS[case]
        diff, note = setup_scenario(case, spec)
        prompt = PROMPT_TEMPLATE.format(diff=diff, case=case)
        print(f"=== 运行 {case} ({note}) ===", flush=True)
        try:
            out = run_claude(prompt)
            (RESULTS_DIR / f"{case}.txt").write_text(out, encoding="utf-8")
            # 打印摘要(结果前 40 行)
            lines = out.strip().splitlines()
            print("\n".join(lines[:12]), flush=True)
            print(f"... (共 {len(lines)} 行, 已存 {RESULTS_DIR / (case + '.txt')})", flush=True)
        except subprocess.TimeoutExpired:
            print(f"{case}: 超时(420s)", flush=True)
        except Exception as e:
            print(f"{case}: 失败 {type(e).__name__}: {e}", flush=True)
    print("全部完成", flush=True)


if __name__ == "__main__":
    main()
