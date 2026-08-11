"""GitHubClient 逻辑单测(mock _get, 零真实网络)。"""

from unittest.mock import MagicMock

from pr_review.github import GitHubClient


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
