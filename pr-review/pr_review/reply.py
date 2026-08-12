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

# 决议意图关键词(P1a: 规则判定, 简单可靠; 命中任一即判定)
RESOLVE_KEYWORDS = ("已修复", "已解决", "修好了", "已处理", "fixed", "resolved", "done", "已改", "已按建议")
IGNORE_KEYWORDS = (
    "设计意图", "故意的", "有意", "不用改", "忽略", "无需处理", "不采纳",
    "by design", "intended", "ignore", "won't fix", "wont fix",
)

# AI 回复中附加的决议标记(HTML 注释, 用户不可见, 下轮 review 扫描用)
RESOLUTION_MARK_RE = r"<!-- pr-review:(resolve|ignore):(.+):(\d+) -->"


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

        # 4. 生成回复;若用户表态(已解决/设计意图),附加决议标记供下轮 review 识别
        reply = self._generate_reply(thread)
        if not reply:
            logger.warning("LLM 返回空回复,跳过")
            return False
        intent = self._resolve_intent(comment.get("body", ""))
        if intent != "ask":
            reply = self._append_resolution_mark(reply, intent, thread)
            logger.info("线程决议: %s(%s:%s)", intent, *self._root_location(thread))
        self.github.post_pull_comment(reply, in_reply_to=comment["id"])
        logger.info("已回复线程 #%s(%d 条对话)", comment["id"], len(thread))
        return True

    # ------------------------------------------------------------------ 决议判定
    @staticmethod
    def _resolve_intent(body: str) -> str:
        """按关键词判定用户意图: ignore(设计意图/忽略) / resolve(已解决) / ask(仅提问)。"""
        low = body.lower()
        if any(k in low for k in IGNORE_KEYWORDS):
            return "ignore"
        if any(k in low for k in RESOLVE_KEYWORDS):
            return "resolve"
        return "ask"

    @staticmethod
    def _root_location(thread: list[dict[str, Any]]) -> tuple[str, int]:
        """线程根评论(AI 审查意见)的 path/line,作为决议定位。"""
        root = thread[0] if thread else {}
        return str(root.get("path", "")), int(root.get("line", 0) or 0)

    def _append_resolution_mark(
        self, reply: str, intent: str, thread: list[dict[str, Any]]
    ) -> str:
        """在回复末尾附加隐藏决议标记(HTML 注释, GitHub 渲染不可见)。"""
        path, line = self._root_location(thread)
        if not path or not line:
            logger.info("线程根评论无 path/line,跳过决议标记")
            return reply
        return f"{reply}\n\n<!-- pr-review:{intent}:{path}:{line} -->"

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
