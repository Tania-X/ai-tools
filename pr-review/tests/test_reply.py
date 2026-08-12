"""ReplyHandler 回复模式单测(mock GitHub 与 LLM, 零真实调用)。"""

from unittest.mock import MagicMock

from gateway import ChatResponse

from pr_review.github import GitHubClient
from pr_review.reply import ReplyHandler
from pr_review.prompt import build_reply_messages

# 评论结构: id / body / user.login / user.type / in_reply_to_id / path / line
AI_COMMENT = {
    "id": 100,
    "body": "## 🤖 AI 代码审查\n\n🟡 **建议使用 get 获取参数**",
    "user": {"login": "github-actions[bot]", "type": "Bot"},
    "in_reply_to_id": None,
    "path": "src/auth.py",
    "line": 10,
}
USER_REPLY = {
    "id": 101,
    "body": "为什么这么判断?",
    "user": {"login": "dev", "type": "User"},
    "in_reply_to_id": 100,
}
BOT_REPLY = {**USER_REPLY, "id": 102, "user": {"login": "github-actions[bot]", "type": "Bot"}}
HUMAN_COMMENT = {
    "id": 103,
    "body": "我觉得这里应该重构",
    "user": {"login": "dev", "type": "User"},
    "in_reply_to_id": None,
}


def _handler(comments=None, llm_content="明白, 已确认是设计意图。"):
    github = MagicMock(spec=GitHubClient)
    github.get_pull_comments.return_value = comments or [AI_COMMENT, USER_REPLY]
    llm = MagicMock()
    llm.chat.return_value = ChatResponse(
        content=llm_content, model="deepseek-chat", provider="deepseek", usage={}
    )
    return ReplyHandler(github=github, llm=llm), github, llm


def test_bot_comment_skipped():
    handler, github, llm = _handler()
    assert handler.handle(BOT_REPLY) is False
    github.post_pull_comment.assert_not_called()
    llm.chat.assert_not_called()


def test_non_reply_comment_skipped():
    handler, github, llm = _handler()
    assert handler.handle(HUMAN_COMMENT) is False  # 无 in_reply_to_id
    llm.chat.assert_not_called()


def test_non_ai_thread_skipped():
    # 用户回复的是人工评论(链上无 AI 标识)
    human_chain = [
        {"id": 200, "body": "手动 review 意见", "user": {"login": "dev"}, "in_reply_to_id": None},
        {"id": 201, "body": "回复", "user": {"login": "dev"}, "in_reply_to_id": 200},
    ]
    handler, github, llm = _handler(comments=human_chain)
    assert handler.handle(human_chain[1]) is False
    llm.chat.assert_not_called()


def test_user_reply_gets_ai_answer():
    handler, github, llm = _handler()
    assert handler.handle(USER_REPLY) is True
    # 纯提问(ask):无决议标记,回复原样发布
    github.post_pull_comment.assert_called_once_with(
        "明白, 已确认是设计意图。", in_reply_to=101
    )


# ---------------------------------------------------------------- P1a 决议驱动
def test_resolve_intent_keywords():
    assert ReplyHandler._resolve_intent("这是设计意图, 忽略") == "ignore"
    assert ReplyHandler._resolve_intent("by design, 不用改") == "ignore"
    assert ReplyHandler._resolve_intent("已按建议修复了") == "resolve"
    assert ReplyHandler._resolve_intent("fixed, thanks") == "resolve"
    assert ReplyHandler._resolve_intent("为什么这么判断?") == "ask"
    assert ReplyHandler._resolve_intent("能举个例子吗") == "ask"


def test_reply_appends_mark_on_ignore():
    """用户表态忽略 → AI 回复附带隐藏决议标记(供下轮 review 识别)。"""
    reply = {**USER_REPLY, "body": "这是设计意图, 忽略"}
    handler, github, _ = _handler()
    assert handler.handle(reply) is True
    posted_body = github.post_pull_comment.call_args[0][0]
    assert "<!-- pr-review:ignore:src/auth.py:10 -->" in posted_body
    assert "明白, 已确认是设计意图。" in posted_body


def test_reply_appends_mark_on_resolve():
    reply = {**USER_REPLY, "body": "已按建议修复了"}
    handler, github, _ = _handler()
    handler.handle(reply)
    posted_body = github.post_pull_comment.call_args[0][0]
    assert "<!-- pr-review:resolve:src/auth.py:10 -->" in posted_body


def test_mark_skipped_when_root_has_no_location():
    """线程根评论无 path/line(异常情况)→ 不附加标记,回复正常发布。"""
    no_loc = {**AI_COMMENT, "path": None, "line": None}
    comments = [no_loc, USER_REPLY]
    reply = {**USER_REPLY, "body": "已解决"}
    handler, github, _ = _handler(comments=comments)
    handler.handle(reply)
    posted_body = github.post_pull_comment.call_args[0][0]
    assert "<!-- pr-review:" not in posted_body


def test_thread_collection_walks_parent_chain():
    # 三级链: 审查意见(100) ← AI 回复(102) ← 用户追问(103)
    chain_comments = [
        AI_COMMENT,
        {**USER_REPLY, "id": 102, "body": "🤖 判断依据是 spec 契约", "in_reply_to_id": 100},
        {**USER_REPLY, "id": 103, "body": "但 spec 里没写这个字段", "in_reply_to_id": 102},
    ]
    handler, _, _ = _handler(comments=chain_comments)
    thread = handler._collect_thread(chain_comments[2])
    assert [c["id"] for c in thread] == [100, 102, 103]  # 旧→新


def test_build_reply_messages_marks_ai_author():
    thread = [
        {**AI_COMMENT, "user": {"login": "github-actions[bot]"}},
        {**USER_REPLY, "user": {"login": "dev"}},
    ]
    messages = build_reply_messages(thread)
    assert messages[0]["role"] == "system"
    assert "简洁" in messages[0]["content"] and "3 句话" in messages[0]["content"]
    user = messages[1]["content"]
    assert "AI 审查" in user and "dev" in user
    assert "建议使用 get 获取参数" in user


def test_reply_max_tokens_limited():
    handler, _, llm = _handler()
    handler.handle(USER_REPLY)
    _, kwargs = llm.chat.call_args
    assert kwargs["max_tokens"] <= 300
