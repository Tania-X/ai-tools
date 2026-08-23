"""本地 CLI: 用 LocalPlatform 跑一次不依赖 GitHub 的代码审查。

用法:
    # DeepSeek(默认 provider)
    AI_GATEWAY_API_KEYS=sk-xxx python -m pr_review.cli --repo /path/to/repo

    # 其他 OpenAI 兼容服务
    python -m pr_review.cli --repo /path/to/repo \
        --provider openai --base-url https://api.openai.com \
        --model gpt-4o --api-key sk-xxx

可选参数:
    --repo PATH
    --base HEAD~1
    --head HEAD
    --config .ai-review.yaml
    --api-key KEY
    --provider NAME
    --base-url URL
    --model MODEL
    --no-thinking
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent   # pr-review/pr_review
_PKG_ROOT = _SCRIPT_DIR.parent                  # pr-review
_REPO_ROOT = _PKG_ROOT.parent                   # ai-tools 根(含 gateway 包)
for _p in (str(_PKG_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway import LLMClient, load_config as load_gateway_config  # noqa: E402

from pr_review.config import load_config as load_review_config  # noqa: E402
from pr_review.context import ContextCollector  # noqa: E402
from pr_review.local_platform import LocalPlatform  # noqa: E402
from pr_review.review import ReviewRunner  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run AI review on a local git diff")
    parser.add_argument("--repo", default=".", help="local git repo path")
    parser.add_argument("--base", default="HEAD~1", help="base revision")
    parser.add_argument("--head", default="HEAD", help="head revision")
    parser.add_argument("--config", default=".ai-review.yaml", help="review config path")
    parser.add_argument("--api-key", default=None, help="LLM API key (overrides AI_GATEWAY_API_KEYS)")
    parser.add_argument("--provider", default=None, help="provider name: deepseek/kimi/openai/custom")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    parser.add_argument("--model", default=None, help="model name")
    parser.add_argument("--no-thinking", action="store_true", help="disable DeepSeek thinking mode")
    return parser


def run_review(args: argparse.Namespace) -> int:
    """执行一次本地 diff 审查(被 pr_review.cli 和 ai-tools-cli 共用)。"""
    # 显式 CLI 参数优先, 转成 gateway 可识别的环境变量
    if args.api_key:
        os.environ["AI_GATEWAY_API_KEYS"] = args.api_key
    if args.provider:
        os.environ["AI_GATEWAY_PROVIDER"] = args.provider
    if args.base_url:
        os.environ["AI_GATEWAY_BASE_URL"] = args.base_url
    if args.model:
        os.environ["AI_GATEWAY_MODEL"] = args.model
    if args.no_thinking:
        os.environ["AI_GATEWAY_EXTRA_BODY"] = '{"thinking": {"type": "disabled"}}'

    platform = LocalPlatform(args.repo, base=args.base, head=args.head)
    review_cfg = load_review_config(args.config)
    context = ContextCollector(
        repo_root=args.repo,
        patterns=review_cfg.context_files,
        max_chars=review_cfg.max_context_chars,
    ).collect()
    llm = LLMClient(load_gateway_config())

    runner = ReviewRunner(
        platform=platform,
        llm=llm,
        config=review_cfg,
        repo_root=args.repo,
        context=context,
    )
    result = runner.run()
    print(runner.format_comment(result))
    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(run_review(args))


if __name__ == "__main__":
    main()
