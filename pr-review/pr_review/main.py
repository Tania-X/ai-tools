"""GitHub Action 入口:读事件 → 加载配置 → 运行审查 → 发评论。

用法(在 action.yml 中):
    python -m pr_review.main

环境变量:
    GITHUB_EVENT_PATH   Action 注入的事件 JSON 文件(含 pull_request 信息)
    GITHUB_REPOSITORY   owner/name
    GITHUB_TOKEN        自动注入,需 pull-requests: write
    AI_GATEWAY_*        走 gateway 配置(见 gateway/config.py)
    AI_REVIEW_CONFIG    .ai-review.yaml 路径(默认仓库根 .ai-review.yaml)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# 脚本方式运行(python pr_review/main.py)时 __package__ 为 None,不能相对导入。
# 手动把包根与仓库根加入 sys.path,统一用绝对导入。
_SCRIPT_DIR = Path(__file__).resolve().parent   # pr-review/pr_review
_PKG_ROOT = _SCRIPT_DIR.parent                  # pr-review
_REPO_ROOT = _PKG_ROOT.parent                   # ai-tools 根(含 gateway 包)
for _p in (str(_PKG_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway import load_config as load_gateway_config  # noqa: E402
from gateway import LLMClient  # noqa: E402

from pr_review.config import load_config as load_review_config  # noqa: E402
from pr_review.github import GitHubClient  # noqa: E402
from pr_review.review import ReviewRunner  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pr_review.main")


def _repo_from_env() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("缺少 GITHUB_REPOSITORY(owner/name)")
    return repo


def _pr_number_from_event() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not Path(event_path).is_file():
        raise SystemExit("缺少 GITHUB_EVENT_PATH,需在 pull_request 事件中运行")
    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)
    number = (event.get("pull_request") or {}).get("number")
    if not number:
        # 兼容 issue_comment / pull_request_review_comment 事件
        number = (event.get("issue") or {}).get("number")
    if not number:
        raise SystemExit("事件中未找到 PR 号,仅支持 pull_request 相关事件")
    return int(number)


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN(Action 会自动注入)")

    repo = _repo_from_env()
    pr_number = _pr_number_from_event()

    # gateway 配置:AI_GATEWAY_CONFIG TOML 或 AI_GATEWAY_* 环境变量
    gw_cfg = load_gateway_config()
    llm = LLMClient(gw_cfg)

    # 审查配置:.ai-review.yaml(默认仓库根)
    review_cfg = load_review_config(os.environ.get("AI_REVIEW_CONFIG", ".ai-review.yaml"))

    with GitHubClient(token=token, repo=repo, pr_number=pr_number) as github:
        runner = ReviewRunner(github=github, llm=llm, config=review_cfg)
        result = runner.run()

        if not result.has_issues and not result.summaries:
            logger.info("没有审查结果,跳过评论")
            return

        body = runner.format_comment(result)
        pr = github.get_pr_info()
        github.post_review(body=body, head_sha=pr.head_sha)

        logger.info(
            "PR #%s 审查完成: %d 个问题, %d 批, token=%d, cost=¥%.4f",
            pr_number,
            len(result.issues),
            result.batches,
            result.total_tokens,
            result.total_cost,
        )


if __name__ == "__main__":
    main()
