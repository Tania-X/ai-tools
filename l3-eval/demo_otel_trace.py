"""OTel 追踪演示: 本地跑一次 agentic 审查, 控制台输出完整 trace。

用法: OTEL_ENABLED=1 DEEPSEEK_API_KEY=<key> python l3-eval/demo_otel_trace.py
依赖: gateway(DeepSeek) + pr-review; 审查 l3-eval/case-refactor-context 场景。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pr-review"))

from gateway.client import GatewayConfig, LLMClient, ProviderConfig
from gateway.otel import setup_tracing
from pr_review.config import QualityConfig, ReviewConfig
from pr_review.diff import parse_diff
from pr_review.github import PRInfo
from pr_review.review import ReviewRunner

KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PR = PRInfo(number=777, title="otel demo", body="", head_sha="x", head_ref="demo", base_ref="main")

PATCH = (
    "diff --git a/l3-eval/case-refactor-context/src/user.go b/l3-eval/case-refactor-context/src/user.go\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/l3-eval/case-refactor-context/src/user.go\n"
    "@@ -0,0 +1,6 @@\n"
    "+// Package main 提供用户示例。\n"
    "+package main\n"
    "+\n"
    "+// getUserName 根据 id 返回用户名。\n"
    "+func getUserName(id string) string {\n"
    "+\treturn \"alice\"\n"
    "+}\n"
)


def main() -> None:
    setup_tracing()  # console exporter(或 OTLP 端点)
    cfg = GatewayConfig(
        providers={
            "deepseek": ProviderConfig(
                name="deepseek",
                base_url="https://api.deepseek.com",
                api_keys=[KEY],
                model="deepseek-v4-flash",
                max_tokens=4096,
                temperature=0.3,
                timeout=120,
            )
        }
    )
    llm = LLMClient(cfg)
    review_cfg = ReviewConfig(quality_gate=QualityConfig(enabled=False))

    class FakeGitHub:
        def get_pr_info(self):
            return PR

        def get_pr_files(self):
            return [{"filename": "l3-eval/case-refactor-context/src/user.go", "status": "added", "patch": PATCH}]

    runner = ReviewRunner(
        github=FakeGitHub(),
        llm=llm,
        config=review_cfg,
        repo_root=str(Path(__file__).resolve().parent.parent),
    )
    print("=== 开始审查(OTEL_ENABLED=1, trace 输出在下方)===\n")
    result = runner.run()
    print(f"\n=== 审查完成: issues={len(result.issues)} token={result.total_tokens} 成本={result.total_cost:.4f}元 ===")


if __name__ == "__main__":
    main()
