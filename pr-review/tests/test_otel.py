"""OTel 追踪埋点测试: 审查链路 span 树完整性与属性。"""

from __future__ import annotations

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway import ChatResponse
from gateway.otel import get_tracer, reset_tracing, setup_tracing
from pr_review.config import QualityConfig, ReviewConfig
from pr_review.github import PRInfo
from pr_review.review import ReviewRunner

PR = PRInfo(
    number=9, title="otel test", body="", head_sha="s", head_ref="x", base_ref="main"
)
FILE_ITEM = {
    "filename": "src/auth.go",
    "status": "modified",
    "patch": (
        "diff --git a/src/auth.go b/src/auth.go\n"
        "--- a/src/auth.go\n"
        "+++ b/src/auth.go\n"
        "@@ -1,3 +1,4 @@\n"
        " package main\n"
        "+// 注释\n"
        " func main() {}\n"
    ),
}
GOOD_JSON = '{"summary": "s", "issues": [{"file": "src/auth.go", "line": 4, "severity": "warn", "title": "t", "detail": "d", "suggestion": "s"}]}'


@pytest.fixture
def spans(tmp_path, monkeypatch):
    """启用内存 exporter, 跑一次审查, 返回全部 spans。"""
    from unittest.mock import MagicMock

    monkeypatch.setenv("OTEL_ENABLED", "1")
    import opentelemetry.trace as _ot

    saved_provider = _ot._TRACER_PROVIDER
    reset_tracing()
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter)

    github = MagicMock()
    github.get_pr_info.return_value = PR
    github.get_pr_files.return_value = [FILE_ITEM]
    llm = MagicMock()
    llm.chat.side_effect = [
        ChatResponse(
            content="",
            model="m",
            provider="p",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            cost=0.001,
            tool_calls=[{"id": "c1", "name": "grep", "arguments": json.dumps({"pattern": "foo"})}],
        ),
        ChatResponse(
            content=GOOD_JSON, model="m", provider="p",
            usage={"prompt_tokens": 20, "completion_tokens": 10}, cost=0.002,
        ),
    ]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.go").write_text("package main\n\nfunc foo() {}\n", encoding="utf-8")
    runner = ReviewRunner(
        github=github, llm=llm, config=ReviewConfig(quality_gate=QualityConfig(enabled=False)),
        repo_root=tmp_path,
    )
    runner.run()
    yield exporter.get_finished_spans()
    # 清理: 恢复原始全局 provider, 避免污染其他测试(Proxy 恢复会递归, 直接赋值)
    _ot._TRACER_PROVIDER = saved_provider
    reset_tracing()


def test_span_tree_complete(spans):
    """根/批次/工具 span 齐全(mock LLM 不产生 llm.chat, 后者单独测)。"""
    names = {s.name for s in spans}
    assert "pr_review.run" in names
    assert "review.batch" in names
    assert "repo_tools.execute" in names


def test_root_span_attributes(spans):
    root = next(s for s in spans if s.name == "pr_review.run")
    assert root.attributes.get("pr.number") == 9
    assert root.attributes.get("pr.files") == 1


def test_llm_chat_span(monkeypatch):
    """真实 LLMClient.chat 的埋点(独立于 mock 的审查流程)。"""
    from unittest.mock import MagicMock

    monkeypatch.setenv("OTEL_ENABLED", "1")
    import opentelemetry.trace as _ot

    saved_provider = _ot._TRACER_PROVIDER
    reset_tracing()
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter)

    from gateway.client import GatewayConfig, LLMClient, ProviderConfig

    pc = ProviderConfig(
        name="test", base_url="https://x", api_keys=["k"], model="m",
        max_tokens=512, temperature=0.7, timeout=30,
    )
    llm = LLMClient(GatewayConfig(providers={"test": pc}))
    llm._call_with_retry = MagicMock(
        return_value=(
            {
                "choices": [{"message": {"content": "hi", "tool_calls": [{"id": "c", "function": {"name": "grep", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            "k",
        )
    )
    llm.chat(
        [{"role": "user", "content": "hi"}],
        provider="test",
        tools=[{"type": "function", "function": {"name": "grep"}}],
    )
    spans = exporter.get_finished_spans()
    llm_spans = [s for s in spans if s.name == "llm.chat"]
    assert len(llm_spans) == 1
    s = llm_spans[0]
    assert s.attributes.get("llm.provider") == "test"
    assert s.attributes.get("llm.tools") is True
    assert s.attributes.get("llm.tool_calls") == 1
    assert s.attributes.get("llm.prompt_tokens") == 10
    assert s.attributes.get("llm.cost") == 0.0
    _ot._TRACER_PROVIDER = saved_provider
    reset_tracing()


def test_tool_span_attributes(spans):
    tool = next(s for s in spans if s.name == "repo_tools.execute")
    assert tool.attributes.get("tool.name") == "grep"
    assert tool.attributes.get("tool.result_len", 0) > 0


def test_parent_child_relation(spans):
    """工具 span 是批次 span 的后代。"""
    tool = next(s for s in spans if s.name == "repo_tools.execute")
    batch = next(s for s in spans if s.name == "review.batch")
    root = next(s for s in spans if s.name == "pr_review.run")
    assert tool.parent is not None
    assert tool.parent.span_id == batch.context.span_id
    assert batch.parent is not None
    assert batch.parent.span_id == root.context.span_id
