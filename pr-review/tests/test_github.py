"""GitHubClient 逻辑单测(mock _get, 零真实网络)。"""

from unittest.mock import MagicMock

from pr_review.github import GitHubClient
from pr_review.types import PRFile


def _client(reviews_per_page: list[list[dict]]):
    client = GitHubClient.__new__(GitHubClient)
    client.repo = "o/r"
    client.pr_number = 1
    mock = MagicMock(side_effect=reviews_per_page)
    client._get = mock
    return client


def test_count_ai_reviews_counts_marker_only():
    client = _client([
        [
            {"body": "## 🤖 AI 代码审查 · 第 1 次评审\n..."},
            {"body": "## 🤖 AI 代码审查 · 第 2 次评审\n..."},
            {"body": "人工 review: 我看了下没问题"},
            {"body": None},
        ]
    ])
    assert client.count_ai_reviews() == 2


def test_count_ai_reviews_empty():
    client = _client([[]])
    assert client.count_ai_reviews() == 0


def test_count_ai_reviews_paginated():
    page1 = [{"body": "## 🤖 AI 代码审查"} for _ in range(100)]
    page2 = [{"body": "## 🤖 AI 代码审查"}, {"body": "human"}]
    client = _client([page1, page2])
    assert client.count_ai_reviews() == 101
    assert client._get.call_count == 2  # 满 100 继续翻页, 不足 100 停止


def test_get_pr_files_returns_prfile():
    client = _client([
        [
            {"filename": "src/app.py", "status": "modified", "patch": "@@ -1 +1 @@", "previous_filename": ""},
            {"filename": "old.py", "status": "renamed", "patch": "", "previous_filename": "src/old.py"},
        ]
    ])
    files = client.get_pr_files()
    assert len(files) == 2
    assert all(isinstance(f, PRFile) for f in files)
    assert files[0].filename == "src/app.py"
    assert files[0].patch == "@@ -1 +1 @@"
    assert files[1].previous_filename == "src/old.py"
