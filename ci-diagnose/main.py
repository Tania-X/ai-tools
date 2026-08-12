"""ci-diagnose 入口:workflow_run 事件 → 失败诊断 → 评论到 PR。

用法(action.yml):
    python main.py

环境变量:
    GITHUB_TOKEN / GITHUB_REPOSITORY / GITHUB_EVENT_PATH(自动注入)
    AI_GATEWAY_*  LLM 配置(同 gateway)
    CI_DIAGNOSE_MAX_LOG_CHARS  日志注入预算(默认 8000)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# 脚本方式运行时把 ai-tools 根加入 sys.path,复用 gateway 包
_SCRIPT_DIR = Path(__file__).resolve().parent       # ci-diagnose
_REPO_ROOT = _SCRIPT_DIR.parent                      # ai-tools 根
for _p in (str(_SCRIPT_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway import load_config as load_gateway_config  # noqa: E402
from gateway import LLMClient  # noqa: E402

from ci_diagnose.client import GitHubClient  # noqa: E402
from ci_diagnose.diagnose import Diagnoser  # noqa: E402
from ci_diagnose.prompt import format_diagnosis_comment  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ci_diagnose.main")


def _load_event() -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not Path(event_path).is_file():
        raise SystemExit("缺少 GITHUB_EVENT_PATH,需在 workflow_run 事件中运行")
    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN(Action 会自动注入)")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("缺少 GITHUB_REPOSITORY(owner/name)")

    event = _load_event()
    run = event.get("workflow_run") or {}
    conclusion = run.get("conclusion", "")
    if conclusion != "failure":
        logger.info("workflow_run 结论为 %s,跳过诊断", conclusion or "unknown")
        return
    # fork 仓库的 run 不诊断(避免污染)
    head_repo = (run.get("head_repository") or {}).get("full_name", "")
    if head_repo and head_repo != repo:
        logger.info("fork 仓库的 run(head=%s),跳过", head_repo)
        return

    run_id = int(run["id"])
    workflow_name = run.get("name", "CI")
    head_sha = run.get("head_sha", "")

    max_log_chars = int(os.environ.get("CI_DIAGNOSE_MAX_LOG_CHARS", "8000") or 8000)
    gw_cfg = load_gateway_config()
    llm = LLMClient(gw_cfg)

    with GitHubClient(token=token, repo=repo) as github:
        # 找 PR(仅评论到 PR;push 到 main 的 run 无 PR 则跳过)
        pr_number = github.find_pr_by_sha(head_sha) if head_sha else None
        if pr_number is None:
            logger.info("未找到对应开放 PR(sha=%s),跳过评论", head_sha)
            return

        # 收集失败 job 日志
        log_parts: list[str] = []
        for job in github.get_workflow_jobs(run_id):
            if job.get("conclusion") == "failure" and job.get("id"):
                try:
                    log_parts.append(github.download_job_logs(int(job["id"])))
                except Exception as e:  # 单个 job 拉取失败不中断
                    logger.warning("拉取 job %s 日志失败: %s", job.get("id"), e)
        if not log_parts:
            logger.warning("未获取到失败日志(job 均无日志或无失败 job)")
            return

        # LLM 诊断
        diag = Diagnoser(llm=llm, max_log_chars=max_log_chars).diagnose(
            workflow_name, run_id, "\n--- job ---\n".join(log_parts)
        )
        body = format_diagnosis_comment(
            diag,
            workflow_name=workflow_name,
            run_id=run_id,
            run_url=run.get("html_url", ""),
            model=gw_cfg.get().model,
            tokens=0,  # 简化: token 统计后续接入
        )
        github.post_issue_comment(pr_number, body)
        logger.info("已评论 PR #%s: %s", pr_number, diag.get("summary", ""))


if __name__ == "__main__":
    main()
