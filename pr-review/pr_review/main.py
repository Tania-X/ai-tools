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
from pr_review.config import ReviewConfig  # noqa: E402
from pr_review.context import ContextCollector  # noqa: E402
from pr_review.github import GitHubClient, GitHubError  # noqa: E402
from pr_review.review import ReviewRunner, ReviewResult  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pr_review.main")


def _has_blocking_issues(cfg: ReviewConfig, result: ReviewResult) -> bool:
    """是否达到合并门禁(fail_on_severity)级别的问题。

    needs_review=true 的问题(设计意图类不确定判断)不计入门禁——避免误报阻塞合并。
    """
    if cfg.fail_on_severity == "off":
        return False
    threshold = cfg.severity_rank(cfg.fail_on_severity)
    return any(
        not issue.needs_review and cfg.severity_rank(issue.severity) <= threshold
        for issue in result.issues
    )


def _check_title(result: ReviewResult, blocked: bool, cfg: ReviewConfig) -> str:
    prefix = f"[第{result.review_no}次] " if result.review_no else ""
    if blocked:
        counts = result.severity_counts
        parts = [f"{counts.get('error', 0)} Error", f"{counts.get('warn', 0)} Warn"]
        return f"{prefix}存在达到门禁级别({cfg.fail_on_severity})的问题: {', '.join(parts)}"
    if result.has_issues:
        return f"{prefix}审查通过(未达到门禁级别)"
    return f"{prefix}审查通过,未发现问题"


def _check_summary(result: ReviewResult, cfg: ReviewConfig) -> str:
    counts = result.severity_counts
    lines = [
        f"- 问题统计: {counts['error']} Error / {counts['warn']} Warn / {counts['info']} Info",
        f"- 批次: {result.batches} | token: {result.total_tokens}",
    ]
    if result.total_cost:
        lines.append(f"- 成本: ¥{result.total_cost:.4f}")
    if result.skipped_files:
        lines.append(f"- 跳过文件: {result.skipped_files}")
    if cfg.fail_on_severity != "off":
        lines.append(f"- 门禁级别: {cfg.fail_on_severity}(存在达到该级别的问题时本 check 失败)")
    return "\n".join(lines)


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


def _mode_from_event() -> str:
    """按事件类型区分运行模式: review(PR 审查) / reply(线程回复)。"""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    return "reply" if event_name == "pull_request_review_comment" else "review"


def _handle_reply_event(github: GitHubClient, llm: LLMClient) -> None:
    """处理用户对 AI 审查评论的线程回复(简洁回答)。"""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not Path(event_path).is_file():
        raise SystemExit("缺少 GITHUB_EVENT_PATH")
    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)
    comment = event.get("comment") or {}
    from .reply import ReplyHandler

    if ReplyHandler(github=github, llm=llm).handle(comment):
        logger.info("已回复线程评论 #%s", comment.get("id"))
    else:
        logger.info("无回复动作(非 AI 线程 / Bot 评论 / 非回复)")


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN(Action 会自动注入)")

    repo = _repo_from_env()
    pr_number = _pr_number_from_event()

    # gateway 配置:AI_GATEWAY_CONFIG TOML 或 AI_GATEWAY_* 环境变量
    gw_cfg = load_gateway_config()
    llm = LLMClient(gw_cfg)

    # 回复模式:不跑审查,只回应线程(简洁,无需 review 配置/上下文)
    if _mode_from_event() == "reply":
        with GitHubClient(token=token, repo=repo, pr_number=pr_number) as github:
            _handle_reply_event(github, llm)
        return

    # 审查配置:.ai-review.yaml(默认仓库根)
    review_cfg = load_review_config(os.environ.get("AI_REVIEW_CONFIG", ".ai-review.yaml"))

    # 仓库上下文(action 环境 cwd=checkout 的仓库根):约定/契约文档摘要注入 prompt
    repo_root = os.getcwd()
    context = ContextCollector(
        repo_root=repo_root,
        patterns=review_cfg.context_files,
        max_chars=review_cfg.max_context_chars,
    ).collect()
    if context:
        logger.info("已收集仓库上下文: %d 字符", len(context))

    with GitHubClient(token=token, repo=repo, pr_number=pr_number) as github:
        runner = ReviewRunner(
            github=github,
            llm=llm,
            config=review_cfg,
            repo_root=repo_root,
            context=context,
        )
        result = runner.run()

        # 评审次数: 已有 AI review 数 + 1(显示"第 N 次评审")
        result.review_no = github.count_ai_reviews() + 1

        if not result.has_issues and not result.summaries:
            logger.info("没有审查结果,跳过评论")
            return

        body = runner.format_comment(result)
        inline = runner.build_inline_comments(result)
        pr = github.get_pr_info()
        github.post_review(body=body, head_sha=pr.head_sha, comments=inline)

        logger.info("行内评论线程: %d 条(其余问题在整体评论)", len(inline))

        # check-run 合并门禁:达到 fail_on_severity 门槛 → failure(check 红)
        # 注意:权限不足(旧 workflow 无 checks: write)时仅告警,不中断已发布的评论
        blocked = _has_blocking_issues(review_cfg, result)
        try:
            github.create_check_run(
                "AI Review",
                head_sha=pr.head_sha,
                conclusion="failure" if blocked else "success",
                title=_check_title(result, blocked, review_cfg),
                summary=_check_summary(result, review_cfg),
            )
        except GitHubError as e:
            logger.warning("创建 check-run 失败(可忽略,评论已发布): %s", e)

        logger.info(
            "PR #%s 审查完成: %d 个问题(error=%d warn=%d), %d 批, token=%d, cost=¥%.4f, blocked=%s",
            pr_number,
            len(result.issues),
            result.severity_counts["error"],
            result.severity_counts["warn"],
            result.batches,
            result.total_tokens,
            result.total_cost,
            blocked,
        )

        if blocked:
            logger.error("存在达到门禁级别(%s)的问题, job 判定失败", review_cfg.fail_on_severity)
            sys.exit(1)


if __name__ == "__main__":
    main()
