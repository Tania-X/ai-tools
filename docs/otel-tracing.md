# OTel 可观测性: 审查链路全环节追踪

> 2026-08-16 · feat/agentic-production 分支 · 生产化第 ② 项

## 设计

审查链路五个环节全部埋点, 形成嵌套 span 树:

```
pr_review.run(根 span: pr.number/title/files)
└── review.batch(批次: batch.no/total/files)
    ├── llm.chat(LLM 往返: provider/model/token/成本/耗时/tool_calls)
    └── repo_tools.execute(工具调用: 工具名/结果大小/耗时)
└── review.quality_gate(judge 判定: verdict)
```

一次真实审查的 trace(演示实测):

```
pr_review.run ─ 41s, issues=1, token=8111
└── review.batch(1/1)
    ├── llm.chat → repo_tools.execute ×2   # 工具探索轮 1
    ├── llm.chat → repo_tools.execute ×2   # 工具探索轮 2
    ├── llm.chat → repo_tools.execute      # 工具探索轮 3
    └── llm.chat → repo_tools.execute      # 工具探索轮 4, 收敛输出
```

## 埋点位置

| 模块 | span | 属性 |
|------|------|------|
| gateway/client.py | `llm.chat` | provider/model/tools/prompt_tokens/completion_tokens/cost/tool_calls/content_len |
| pr_review/review.py run() | `pr_review.run` | pr.number/title/files |
| pr_review/review.py _review_batch | `review.batch` | batch.no/total/files |
| pr_review/review.py _chat_with_tools | `repo_tools.execute` | tool.name/result_len |
| pr_review/review.py _quality_loop | `review.quality_gate` | gate.verdict |

## 启用与导出

```bash
# 控制台导出(演示/本地排查)
OTEL_ENABLED=1 python l3-eval/demo_otel_trace.py

# OTLP 导出(接 Jaeger / Collector / 云厂商)
OTEL_ENABLED=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  DEEPSEEK_API_KEY=<key> python l3-eval/demo_otel_trace.py
```

默认关闭(OTEL_ENABLED 未设 → no-op tracer, 零开销), 不影响现有审查与 CI。

## 面试叙事

> 审查引擎接入 OpenTelemetry: 每一次审查的 LLM 往返、工具调用、批次与质量门
> 全部有 span 可追踪——线上出问题时, 能回答"这轮审查为什么慢/为什么降级/
> 模型调了哪些工具", 生产系统可观测性的一环。

## 测试

`pr-review/tests/test_otel.py`: 内存 exporter 验证 span 树完整性、父子关系、
关键属性(llm/tool/root), 全量 191 测试全绿。
