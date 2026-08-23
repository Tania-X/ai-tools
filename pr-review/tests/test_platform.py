"""平台抽象层测试: ReviewRunner 应能通过 platform 接口驱动, 不绑定 GitHubClient。"""

from __future__ import annotations

from unittest.mock import MagicMock

from gateway import ChatResponse

from pr_review.config import QualityConfig, ReviewConfig
from pr_review.review import ReviewRunner
from pr_review.types import PRInfo

PR = PRInfo(
    number=10,
    title="feat: platform decouple",
    body="",
    head_sha="sha",
    head_ref="feat/platform",
    base_ref="main",
)

FILE_ITEM = {
    "filename": "src/app.py",
    "status": "modified",
    "patch": (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def main():\n"
        "+    print('hello')\n"
    ),
}


def test_review_runner_accepts_platform():
    platform = MagicMock()
    platform.get_pr_info.return_value = PR
    platform.get_pr_files.return_value = [FILE_ITEM]

    llm = MagicMock()
    llm.config.get.return_value = MagicMock(model="deepseek-chat")
    llm.chat.return_value = ChatResponse(
        content='{"summary": "ok", "issues": []}',
        model="deepseek-chat",
        provider="deepseek",
        usage={},
        cost=0.0,
    )

    runner = ReviewRunner(
        platform=platform,
        llm=llm,
        config=ReviewConfig(quality_gate=QualityConfig(enabled=False)),
    )
    result = runner.run()

    assert result.model == "deepseek-chat"
    assert result.batches == 1
    platform.get_pr_info.assert_called_once()
    platform.get_pr_files.assert_called_once()
