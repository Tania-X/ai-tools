"""本地 CLI: 用 LocalPlatform 跑一次不依赖 GitHub 的代码审查。

用法:
    AI_GATEWAY_API_KEYS=sk-xxx python -m pr_review.cli --repo /path/to/repo

可选参数:
    --base HEAD~1
    --head HEAD
    --config .ai-review.yaml
"""

from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AI review on a local git diff")
    parser.add_argument("--repo", default=".", help="local git repo path")
    parser.add_argument("--base", default="HEAD~1", help="base revision")
    parser.add_argument("--head", default="HEAD", help="head revision")
    parser.add_argument("--config", default=".ai-review.yaml", help="review config path")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
