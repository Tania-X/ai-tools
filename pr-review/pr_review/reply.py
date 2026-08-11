"""回复模式(P0 交互):用户在行内评论线程回复时,AI 在线程内简洁回答。

触发:workflow 监听 pull_request_review_comment(created) 事件, main.py 按
GITHUB_EVENT_NAME 进入本模块。核心逻辑:

1. 防自触发: GitHub Bot 用户(含 AI 自己的回复)跳过,避免死循环
2. 非回复(无 in_reply_to_id)或非 AI 线程(链上无 REVIEW_MARKER)跳过
3. 收集线程父链(用户回复 → AI 审查意见 → 更早对话)作对话上下文
4. 调 LLM(简洁 prompt + max_tokens 限制) → post_pull_comment 回到线程
"""

from __future__ import annotations

import logging
from typing import Any

from gateway import LLMClient

from .github import GitHubClient, REVIEW_MARKER
from .prompt import build_reply_messages

logger = logging.getLogger("pr_review.reply")

# 回复 token 上限(配合 prompt 的简洁要求,双重限制长度)
REPLY_MAX_TOKENS = 300
# 线程父链最大深度(防异常长链)
MAX_THREAD_DEPTH = 20


class ReplyHandler:
    """处理一条用户对 AI 审查评论的回复。"""

    def __init__(self, github: GitHubClient, llm: LLMClient):
        self.github = github
        self.llm = llm

    def handle(self, comment: dict[str, Any]) -> bool:
        """处理用户回复;返回 True 表示已生成并发布 AI 回复。"""
        # 1. 防自触发:Bot 评论(AI 自己的回复)不处理
        if (comment.get("user") or {}).get("type") == "Bot":
            logger.info("跳过 Bot 评论(防自触发)")
            return False

        # 2. 必须是对某条评论的回复
        reply_to_id = comment.get("in_reply_to_id")
        if not reply_to_id:
            logger.info("跳过:非回复评论(无 in_reply_to_id)")
            return False

        # 3. 收集线程父链,并确认是 AI 的线程
        thread = self._collect_thread(comment)
        if not thread or not any(REVIEW_MARKER in c.get("body", "") for c in thread):
            logger.info("跳过:非 AI 审查线程")
            return False

        # 4. 生成并发布回复
        reply = self._generate_reply(thread)
        if not reply:
            logger.warning("LLM 返回空回复,跳过")
            return False
        self.github.post_pull_comment(reply, in_reply_to=comment["id"])
        logger.info("已回复线程 #%s(%d 条对话)", comment["id"], len(thread))
        return True

    # ------------------------------------------------------------------ 内部
    def _collect_thread(self, comment: dict[str, Any]) -> list[dict[str, Any]]:
        """从用户回复向上收集父链,按时间正序返回(旧→新)。"""
        all_comments = self.github.get_pull_comments()
        by_id = {c["id"]: c for c in all_comments}

        chain: list[dict[str, Any]] = [comment]
        cid = comment.get("in_reply_to_id")
        depth = 0
        while cid and cid in by_id and depth < MAX_THREAD_DEPTH:
            chain.append(by_id[cid])
            cid = by_id[cid].get("in_reply_to_id")
            depth += 1
        chain.reverse()  # 旧→新
        return chain

    def _generate_reply(self, thread: list[dict[str, Any]]) -> str:
        messages = build_reply_messages(thread)
        resp = self.llm.chat(messages, max_tokens=REPLY_MAX_TOKENS)
        return resp.content.strip()
