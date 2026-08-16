# ai-tools — agentic 代码审查引擎(自建 CodeRabbit 替代)

> **自建 AI 代码审查引擎**: 量化评测闭环 + agentic 工具调用 + 生产化三件套(MCP / OTel / 模型路由)。
> 已在 devops-dashboard 真实 PR 落地验证, 同一评测集双引擎对比 9/9 对齐 Claude Code。

---

## 亮点(为什么值得看)

1. **量化评测闭环** —— 不是拍脑袋调 prompt: 9 个人造场景 + 真实 PR 回放集, 精确率/召回率思维,
   Level 0/1 全绿, 同一套评测集可评测**第三方 agent**(双引擎对比)。
2. **agentic 工具调用** —— 函数调用 + 4 工具(read_file/grep/ast_grep/list_dir) + 多轮 tool loop,
   自动收敛 + 硬上限; 对照 CodeRabbit 的取舍(函数调用 vs bash)有调研有落地。
3. **生产化三件套** —— ①审查工具封装为标准 **MCP server**(Claude Code/Codex 可直接调用);
   ②**OpenTelemetry** 全链路追踪(LLM 往返/工具调用/质量门可见); ③**模型路由**(审查强模型/judge 便宜模型)
   + golden 评测跑在 CI。
4. **真实落地** —— devops-dashboard PR#7/8(DDD 重构)跑通: AI 主动查代码确认重构一致性,
   零误报; 四次失败模式的工程化收敛(跑偏/空输出/工具超限/文本工具调用)。

## 架构

```
gateway/        多 provider LLM 网关(DeepSeek/Kimi/OpenAI 兼容, 多 key 轮询/重试/成本统计/OTel 埋点)
pr-review/      GitHub Action 审查引擎
  ├─ pr_review/    diff 解析 → agentic 审查(工具循环) → 质量门(judge+重写) → 评论/门禁
  ├─ mcp_server.py  仓库工具集封装为标准 MCP(stdio, Claude Code 可调用)
  └─ tests/        单测 + OTel span 树验证
golden-tests/   量化评测驱动器(9 场景 + 真实 PR 回放, Level 0/1)
l3-eval/        双引擎对比评测(Claude Code + MCP vs 自研) + OTel 可视化
docs/           设计/评测方案/taste 沉淀/双引擎对比报告/OTel 文档
```

## 评测成绩

**Level 0(9/9 全绿)**: 正样本全命中(case-bug/security/convention/merge-locations/severity-security/refactor-context),
负样本零误报(case-clean/docs), 边界正确(case-bait 不报 error)。

**双引擎对比(9/9 对齐)**: 同一场景集, Claude Code + 我们的 MCP(零策略注入)与自研引擎(带完整策略)审查质量一致,
且严重度判断**自发**符合我们沉淀的策略(必然触发→error, 假设路径→warn)。
→ 详见 [docs/l3-dual-engine-eval.md](docs/l3-dual-engine-eval.md)

## 生产化三件套

| 项 | 成果 | 验证 |
|----|------|------|
| MCP 封装 | repo_tools 4 工具 → 标准 MCP(stdio) | Claude Code 实测调用 + 双引擎对比 9/9 |
| OTel 追踪 | 审查链路 5 环节 span 树, **正式接入远程 Jaeger**(每次 ai-review 自动留痕) | 可视化瀑布图 + span 树单测 + PR#12 审查 trace 实测 |
| 模型路由 + 评测 CI | judge 独立 provider; golden 评测自动化 | judge 路由单测; golden-eval workflow |

### 可观测性落地(正式服务)

- **Jaeger 部署于远程服务器**(badger 持久化 + Caddy basic auth 认证), 每次 PR 审查自动上报 trace
  —— LLM 往返 / 工具调用 / 质量门判定全部留痕;
- 上报方式: action 配置 `otel-enabled / otel-endpoint / otel-service-name / otel-headers` 四个参数;
- 部署 / 迁移 / 运维: [ops/jaeger/README.md](ops/jaeger/README.md)(一键部署 `deploy.sh`、数据迁移 `migrate.sh`)

## 快速开始(接入任意仓库)

```yaml
# .github/workflows/ai-review.yml
name: AI Review
on:
  pull_request:
    types: [opened, synchronize]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Tania-X/ai-tools/pr-review@main
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
```

## 文档索引

- [审查引擎设计](docs/ai-review-architecture.md) · [质量门设计](docs/pr-review-quality-gate.md) · [评测方案](docs/golden-testing.md)
- [双引擎对比报告](docs/l3-dual-engine-eval.md) · [OTel 可观测性](docs/otel-tracing.md)
- [taste 沉淀(审查品味工程化)](docs/taste-engineering.md)

## 为什么这么做(面试叙事)

> 从"自建 CodeRabbit 替代"出发, 一路做到: 量化评测闭环(9 场景 + 真实 PR 回放) →
> agentic 工具调用(对照 CodeRabbit 取舍) → 工具封装 MCP 与主流 agent 互操作(双引擎对比 9/9) →
> 生产化(OTel 可观测 + 模型路由 + 评测 CI)。每一步都有证据, 不是功能堆砌。
