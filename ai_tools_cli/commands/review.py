"""ai-tools-cli review 子命令。"""

from __future__ import annotations

import argparse

from pr_review.cli import run_review


def add_review_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "review",
        help="Run AI review on a local git diff",
        description="使用 LocalPlatform 对本地 git 仓库的 base..head 执行 AI 审查",
    )
    parser.add_argument("--repo", default=".", help="local git repo path")
    parser.add_argument("--base", default="HEAD~1", help="base revision")
    parser.add_argument("--head", default="HEAD", help="head revision")
    parser.add_argument("--config", default=".ai-review.yaml", help="review config path")
    parser.add_argument("--api-key", default=None, help="LLM API key (overrides AI_GATEWAY_API_KEYS)")
    parser.add_argument("--provider", default=None, help="provider name: deepseek/kimi/openai/custom")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    parser.add_argument("--model", default=None, help="model name")
    parser.add_argument("--no-thinking", action="store_true", help="disable DeepSeek thinking mode")
    parser.set_defaults(func=run_review)
