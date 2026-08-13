"""Golden 测试编排: 逐 case 执行 Level 0/1 流程。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .assert_result import evaluate
from .github_api import GitHubAPI
from .gitops import GitOps
from .parser import find_ai_review_comment, parse_comment_issues

POLL_TIMEOUT = 300  # 单次 review 轮询超时(秒)


class GoldenRunner:
    def __init__(
        self,
        *,
        git: GitOps,
        api: GitHubAPI,
        scenarios_dir: Path,
        results_dir: Path,
        level: int = 0,
        cases: list[str] | None = None,
        resume: bool = False,
    ):
        self.git = git
        self.api = api
        self.scenarios_dir = scenarios_dir
        self.results_dir = results_dir
        self.level = level
        self.only_cases = cases
        self.resume = resume

    # ------------------------------------------------------------------ 入口
    def run(self) -> list[dict[str, Any]]:
        manifest = self._load_manifest()
        results: list[dict[str, Any]] = []
        self.results_dir.mkdir(parents=True, exist_ok=True)

        for entry in manifest["cases"]:
            name = entry["name"]
            if self.only_cases and name not in self.only_cases:
                continue
            if self.resume and self._result_exists(name):
                print(f"[skip] {name} 已有结果, 跳过(断点续跑)")
                continue
            print(f"\n=== 运行 case: {name} (category={entry['category']}, level={self.level}) ===")
            result = self._run_case(name, entry)
            results.append(result)
            self._save_result(name, result)

        return results

    # ------------------------------------------------------------------ 单 case
    def _run_case(self, name: str, entry: dict) -> dict[str, Any]:
        expected = self._load_expected(name)
        branch = f"test/{name}"
        category = entry.get("category", "positive")
        run_level1 = self.level >= 1 and category == "positive"

        try:
            # ---- Level 0: buggy 快照 → PR → review → 断言 ----
            self.git.checkout_main()
            self.git.new_branch(branch)
            self.git.apply_snapshot(self.scenarios_dir / name / "changes")
            self.git.commit(f"test: {name}")
            self.git.push(branch)

            pr = self.api.create_pr("main", branch, f"test: {name}")
            pr_number = pr["number"]
            sha = pr["head"]["sha"]

            conclusion = self.api.poll_check_run(sha, timeout=POLL_TIMEOUT)
            if conclusion is None:
                return {"case": name, "status": "skip", "reason": f"轮询超时({POLL_TIMEOUT}s)"}

            body = find_ai_review_comment(self.api.list_issue_comments(pr_number))
            actual = parse_comment_issues(body)
            result = evaluate(name, expected, actual, conclusion)
            result["expected"] = expected.get("expect", {})

            # ---- Level 1: fixed 快照 → push → review#2 → 断言 agree ----
            if run_level1:
                agree, conclusion2 = self._run_fixed_phase(name, branch, pr_number)
                result["level1"] = {"agree": agree, "check_conclusion": conclusion2}
                if not agree:
                    result["pass"] = False
                    result["failures"].append(f"修复后仍未放行(check={conclusion2})")
        finally:
            # 清理(无论成功失败): 关 PR + 删分支
            try:
                self.api.close_pr(pr_number)
            except Exception:
                pass
            self.git.delete_remote_branch(branch)
            self.git.checkout_and_cleanup(branch)

        return result

    def _run_fixed_phase(self, name: str, branch: str, pr_number: int) -> tuple[bool, str | None]:
        """应用 fixed 快照 → push → 轮询 review#2, 返回 (是否放行, conclusion)。"""
        self.git.apply_snapshot(self.scenarios_dir / name / "fixed")
        self.git.commit("fix review findings")
        self.git.push(branch)
        # push 后取新 head sha
        new_sha = self._head_sha(branch)
        conclusion2 = self.api.poll_check_run(new_sha, timeout=POLL_TIMEOUT)
        agree = conclusion2 in ("success", "neutral")  # 非 failure 即放行
        return agree, conclusion2

    def _head_sha(self, branch: str) -> str:
        # 通过 PR 列表或 git rev-parse 拿最新 sha; 这里用 git(驱动器有本地仓库)
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.git.repo_dir), "rev-parse", branch],
            capture_output=True, text=True, check=True,
        )
        return proc.stdout.strip()

    # ------------------------------------------------------------------ 加载
    def _load_manifest(self) -> dict:
        return json.loads((self.scenarios_dir / "manifest.json").read_text(encoding="utf-8"))

    def _load_expected(self, name: str) -> dict:
        return json.loads((self.scenarios_dir / name / "expected.json").read_text(encoding="utf-8"))

    def _result_exists(self, name: str) -> bool:
        return (self.results_dir / f"{name}.json").is_file()

    def _save_result(self, name: str, result: dict) -> None:
        (self.results_dir / f"{name}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
