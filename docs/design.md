# AI × DevOps 工具集 — 设计文档

> 状态：设计稿（起步阶段）
> 创建：2026-08-11
> 关联项目：[devops-dashboard](https://gitee.com/Max_1996/devops-dashboard)（业务层，被集成方）

---

## 一、背景与目标

### 1.1 背景

- 用户学习 AI 应用开发，希望有一套 **AI × DevOps（运维）** 相关的实践项目
- 起因：CodeRabbit（AI PR 审查）是免费试用，不打算付费——想自建/自接 API token 的替代方案
- 已有基础：`devops-dashboard`（Go + Gin + React 运维监控，含告警引擎/日志/指标采集/Webhook 推送）

### 1.2 目标

1. 建立一套**独立于 devops-dashboard 的 AI 能力层**（新仓库 ai-tools），可服务所有项目
2. 覆盖 AI 运维典型场景：PR 审查、告警解读、日志分析、自然语言查询
3. 所有能力统一接入 LLM（DeepSeek / Kimi / OpenAI 兼容），换模型只改一处
4. 最终通过 **MCP Server** 统一暴露，供 AI 助手（WorkBuddy / Claude 等）调用
5. 全程自己动手、自己控制成本（DeepSeek token 级别，近乎免费）

### 1.3 非目标

- 不做大规模分布式/多租户（个人学习项目，单机 + Action 足够）
- 不依赖付费 AI review 服务（CodeRabbit Pro 等）
- 不在 devops-dashboard 内实现 AI 逻辑（保持职责分离）

---

## 二、仓库关系

```
devops-dashboard(业务层, 现有)
   │  提供数据: 告警 / 日志 / 指标 / 部署状态(webhook / API)
   ▼
ai-tools(AI 能力层, 本仓库)
   ├── pr-review       → 服务所有仓库(包括 dashboard 自己)
   ├── alert-explain   → 消费 dashboard 告警
   ├── log-analyzer    → 消费 dashboard 日志
   ├── ops-query       → 查询 dashboard 指标/状态
   └── mcp-server      → 统一暴露给 AI 助手
   ▲
   │  统一调模型
ai-gateway(LLM 网关, 本仓库子模块或独立模块)
```

**原则**：ai-tools 不依赖 dashboard 的代码实现，只依赖其"数据/接口"；dashboard 反过来也不依赖 ai-tools。两边各自演进。

---

## 三、模块规划

| 模块 | 一句话 | 核心学习点 | 规模 |
|------|--------|-----------|------|
| **gateway** | LLM 统一网关：多模型抽象、key 管理、重试、成本统计 | OpenAI 兼容 API、配置、错误处理 | 小 |
| **pr-review** | GitHub Action：PR 打开时取 diff → LLM 审查 → 发 PR 评论 | GitHub Actions、diff 提取、prompt 工程、GitHub API | 中 |
| **alert-explain** | 告警解读：CPU 95% → LLM 生成可能原因 + 排查建议 | 结构化输出(JSON)、与 webhook 集成 | 中 |
| **log-analyzer** | 日志分析：异常日志摘要、根因线索 | 文本切片/摘要、上下文管理 | 中 |
| **ops-query** | 自然语言查指标："昨天 CPU 最高是什么时候" | 意图识别、Tool Use(function calling) | 中大 |
| **mcp-server** | 把以上能力注册成 MCP 工具，供 AI 助手调用 | MCP 协议、工具注册 | 收尾 |

### 3.1 各模块详细设计

#### gateway（地基，先做）

```go
// 统一接口:所有 ai-tools 只依赖这一个包
type Client interface {
    Chat(ctx context.Context, req ChatRequest) (*ChatResponse, error)
}
// 支持:DeepSeek / Kimi / OpenAI(兼容端点, base_url + api_key 可配)
// 能力:多 key 轮询、指数退避重试、token/成本统计、超时
```

- 技术栈：Go（与 dashboard 一致，复用经验；单二进制部署方便）
- 配置：环境变量/配置文件（provider、base_url、api_key、model、max_tokens、temperature）

#### pr-review（第一个可交付）

```
工作流:
1. GitHub Action 监听 pull_request(opened / synchronize)
2. 脚本取 PR 的 diff(github.event + git diff 或 API)
3. 组装 prompt(带项目约定/上下文) → 调 gateway → LLM 输出 review 意见
4. 用 GITHUB_TOKEN 发 PR 评论(或 check-run)

配置:
- .ai-review.yaml(审查重点、忽略路径、指令)——类似 .coderabbit.yaml
```

- 关键设计：diff 可能很大，需要切片/摘要；输出结构化为 JSON（文件 + 行号 + 问题 + 建议）
- 与 CodeRabbit 的关系：自建替代，功能从最小可用起步（先评论，再逐步加）

#### alert-explain（最贴合现有项目）

```
dashboard 告警 → webhook 推送 → ai-alert-explain 服务
  → 调 LLM: "[告警] CPU 95% 持续 10 分钟, 内存 87%"
  → 输出: 可能原因 / 排查步骤 / 建议命令(JSON 结构化)
  → 回推钉钉/企微 或 dashboard 告警详情展示
```

- 复用 dashboard 的 AlertBus / Webhook 通知基础设施

#### ops-query（最有趣）

```
用户提问: "昨天 CPU 最高是什么时候? 哪些进程占用最多?"
  → 意图识别 → 工具调用(dashboard 指标 API) → 组装回答
```

- 核心：Tool Use / function calling（LLM 决定调哪个工具、传什么参数，拿到结果再回答）

#### mcp-server（收尾，统一暴露）

- 把 pr-review / alert-explain / log-analyzer / ops-query 注册为 MCP 工具
- 效果：WorkBuddy / Claude 等 AI 助手可直接调用这些能力

---

## 四、技术选型

| 项 | 选择 | 理由 |
|----|------|------|
| 语言 | Go 1.24+ | 与 dashboard 统一技术栈、部署单二进制、用户已熟 |
| LLM 接入 | DeepSeek / Kimi（OpenAI 兼容 API） | 便宜、国内可直连、统一协议 |
| PR 审查载体 | GitHub Actions + 脚本 | 零部署成本，事件驱动 |
| 服务形态 | 独立 HTTP 服务（alert-explain / ops-query）+ Action（pr-review） | 各模块按需选择 |
| MCP | 官方 MCP SDK（Go） | 标准协议，AI 助手通用 |
| 配置 | YAML + 环境变量 | 与现有项目一致 |

---

## 五、学习路径与里程碑

| 里程碑 | 内容 | 产出 |
|--------|------|------|
| M1 | gateway 完成 + 单测 | 统一 LLM 调用能力 |
| M2 | pr-review 首个 Action 跑通，在 dashboard PR 上出评论 | 自建 AI review 可用 |
| M3 | alert-explain 接入 dashboard 告警，推送解读 | AI 运维首个闭环 |
| M4 | log-analyzer / ops-query | 工具集扩展 |
| M5 | mcp-server 统一暴露 | 可被 AI 助手调用 |

每个里程碑独立 commit、跑通验证再继续；优先 M1 → M2（pr-review 是最初动机）。

---

## 六、目录结构（规划）

```
ai-tools/
├── README.md
├── docs/
│   └── design.md          # 本文件
├── gateway/               # LLM 统一网关(先做)
│   ├── client.go
│   ├── config.go
│   └── client_test.go
├── pr-review/             # GitHub Action 审查(第二个做)
│   ├── action.yml
│   ├── review.go(或 .py)
│   └── .ai-review.yaml.example
├── alert-explain/         # 告警解读
├── log-analyzer/          # 日志分析
├── ops-query/             # 自然语言查询
├── mcp-server/            # MCP 统一暴露
└── .gitignore
```

---

## 七、待定问题

- [ ] gateway 放本仓库子模块还是独立仓库？（倾向：本仓库子模块，先跑起来再说）
- [ ] pr-review 的 review 深度/噪音控制策略（参考 .coderabbit.yaml 的经验：先 chill）
- [ ] alert-explain 的触发方式：webhook 直连 vs dashboard 集成
- [ ] ops-query 的数据源接口是否依赖 dashboard 的 API 契约（需 dashboard 侧配合暴露查询接口）
