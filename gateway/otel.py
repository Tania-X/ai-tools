"""OpenTelemetry 追踪初始化(生产化第 ② 项, 2026-08-16)。

可观测性设计: 审查链路全环节埋点 —
  pr_review.run(根 span)
    ├── review.batch(批次: 文件数/批次号)
    │     ├── llm.chat(LLM 往返: provider/model/token/耗时/成本)
    │     └── repo_tools.execute(工具调用: 工具名/结果大小/耗时)
    └── review.quality_gate(judge 打分/重写)

导出:
- 默认 console exporter(打印 trace 到 stdout, 演示/本地排查)
- 设置 OTEL_EXPORTER_OTLP_ENDPOINT 后改用 OTLP(接 Jaeger/Collector)
- 设置 OTEL_ENABLED=1 启用(默认关闭, 不影响现有运行)
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

_tracer: trace.Tracer | None = None
_setup_done = False


def setup_tracing(span_exporter=None) -> None:
    """配置 TracerProvider 与 exporter(幂等)。

    span_exporter 注入(测试用); 默认 console; OTLP 端点存在则用 OTLP。
    """
    global _setup_done, _tracer
    if _setup_done:
        return
    _setup_done = True

    provider = TracerProvider()
    if span_exporter is not None:
        exporter = span_exporter
    else:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=endpoint)
        else:
            exporter = ConsoleSpanExporter()
    if span_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = provider.get_tracer("ai-tools")


def reset_tracing() -> None:
    """重置追踪配置(测试用, 使 setup_tracing 可再次配置)。"""
    global _setup_done, _tracer
    _setup_done = False
    _tracer = None


def get_tracer() -> trace.Tracer:
    """获取 tracer。未启用(OTEL_ENABLED 未设)时返回 no-op tracer(零开销)。"""
    if os.environ.get("OTEL_ENABLED") == "1":
        if not _setup_done:
            setup_tracing()
        return _tracer or trace.get_tracer("ai-tools")
    return trace.get_tracer("ai-tools-noop")
