"""golden 测试驱动器 CLI 入口。

用法:
    GITHUB_TOKEN=xxx python golden-tests/run_golden_tests.py \
        --repo-dir ../ai-review-golden-tests [--level 0|1] [--cases case-bug] [--dry-run]

    --repo-dir   golden 测试仓库的本地 clone 路径(必填)
    --level      测试层级(0=快照评测, 1=含修复闭环)
    --cases      逗号分隔的 case 白名单(默认全部)
    --resume     断点续跑(跳过已有 results/<case>.json 的 case)
    --dry-run    只打印将执行的步骤, 不实际调用网络
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 把 golden-tests 目录加入 sys.path(目录名含连字符, 不能直接 import)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from golden.github_api import GitHubAPI  # noqa: E402
from golden.gitops import GitOps  # noqa: E402
from golden.report import render_report  # noqa: E402
from golden.runner import GoldenRunner  # noqa: E402

DEFAULT_REPO = "Tania-X/ai-review-golden-tests"


def main() -> int:
    parser = argparse.ArgumentParser(description="golden 测试驱动器")
    parser.add_argument("--repo-dir", required=True, help="golden 仓库本地 clone 路径")
    parser.add_argument("--level", type=int, default=0, choices=[0, 1])
    parser.add_argument("--cases", default="", help="逗号分隔的 case 白名单")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub 仓库 owner/name")
    parser.add_argument("--token", default="", help="GitHub PAT(默认读 GITHUB_TOKEN 环境变量)")
    parser.add_argument("--resume", action="store_true", help="断点续跑")
    parser.add_argument("--dry-run", action="store_true", help="只打印步骤, 不实际执行")
    parser.add_argument("--playback", action="store_true", help="跑真实 PR 回放集(playback/ 目录)")
    args = parser.parse_args()

    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        print("错误: 需要 GitHub PAT(设置 GITHUB_TOKEN 环境变量或 --token)")
        return 2

    repo_dir = Path(args.repo_dir).resolve()
    scenarios_dir = repo_dir / ("playback" if args.playback else "scenarios")
    if not scenarios_dir.is_dir():
        print(f"错误: 找不到场景目录 {scenarios_dir}")
        return 2

    cases = [c.strip() for c in args.cases.split(",") if c.strip()] or None
    results_dir = repo_dir / "results"

    # dry-run: 只打印将执行的计划, 不调用网络
    if args.dry_run:
        runner = GoldenRunner(
            git=None, api=None, scenarios_dir=scenarios_dir, results_dir=results_dir,
            level=args.level, cases=cases, resume=args.resume,
        )
        entries = runner._load_cases()
        print("[dry-run] 将执行以下 case 的测试(Level %d):\n" % args.level)
        for entry in entries:
            name = entry["name"]
            if cases and name not in cases:
                continue
            category = entry.get("category", "positive")
            run_level1 = args.level >= 1 and category == "positive"
            steps = [
                f"建分支 test/{name}",
                f"应用 changes/(buggy 快照) → commit → push",
                "开 PR → 轮询 AI Review check-run",
                "拉评论 → 解析 issues → 断言 expected → 存 results/%s.json" % name,
            ]
            if run_level1:
                steps.append("应用 fixed/ → push → 轮询 review#2 → 断言 agree")
            steps.append("关 PR + 删分支")
            print(f"- {name} [{category}]:")
            for s in steps:
                print(f"    {s}")
        print("\n[dry-run] 完成(未执行任何网络/git 操作)")
        return 0

    api = GitHubAPI(token, args.repo)
    git = GitOps(repo_dir, dry_run=False)

    try:
        runner = GoldenRunner(
            git=git, api=api, scenarios_dir=scenarios_dir, results_dir=results_dir,
            level=args.level, cases=cases, resume=args.resume,
        )
        results = runner.run()
    finally:
        api.close()

    report = render_report(results, args.level)
    report_path = repo_dir / "results" / f"report-level{args.level}.md"
    report_path.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n报告已写入: {report_path}")

    failed = [r for r in results if not r.get("pass") and r.get("status") != "skip"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
