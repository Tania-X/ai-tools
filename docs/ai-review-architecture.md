# ai-review 企业级架构演进(规划)

> 状态:**方案文档**(2026-08-14 讨论产出),未实施
> 定位:从"个人可用 MVP"演进到"可整合企业级私有化部署大模型的 ai-review"的架构蓝图
> 关联:pr-review(当前实现)· golden-tests(评测)· [design.md](design.md)(总体设计)

---

## 一、现状(2026-08)

```
当前形态(可用 MVP):
  GitHub PR 事件 → pr-review(GitHub Action 单体)
    ├─ 读事件/GitHub API(diff、评论、check-run)
    ├─ 上下文收集(AGENTS.md/README/docs, 本地文件)
    ├─ 调 LLM(gateway, Chat Completions 兼容) → JSON issues
    └─ 质量门(judge 打分 + 自检重写) → 评论 + 门禁
  golden-tests: 6 场景量化评测(Level 0/1 全绿, 闭环自洽)
```

**成绩**:审查链路完整(P0 交互 / P1a 决议 / P1b 质量门 / 上下文注入 / 门禁)、量化验证 6/6、已接入 devops-dashboard。

**局限**:
- 单入口:只服务 GitHub Action,无 CLI / IDE / 本地入口
- 上下文源有限:只读仓库本地文件,未接工单/监控/文档等外部系统
- 单模型:审查/蒸馏/judge 共用同一模型配置,无路由
- 紧耦合 GitHub API:review 引擎与平台绑定,无法复用

## 二、Responses API 结论(不升级,保持 Chat Completions)

调研结论(2026-07-31 DeepSeek V4-Flash-0731 发布):

| 事实 | 说明 |
|------|------|
| DeepSeek 已支持 Responses API | 2026-07-31 起,但**仅 deepseek-v4-flash**,v4-pro 为"2026-08 初"承诺 |
| 设计目标 | 为 Codex 类 agent:工具调用(web_search / apply_patch)、有状态会话 |
| 对审查场景的增量价值 | **≈ 0**——审查是"纯文本进 → JSON 出",不用工具、不需要状态 |
| 关键限制 | **不支持 MCP 工具块**(file_search/code_interpreter/MCP tools 被忽略) |
| Chat Completions 的定位 | 私有化部署(vLLM / Ollama / 各类网关)最通用兼容格式 |

**结论**:gateway 保持 Chat Completions 兼容;真正值得关注的是 **Anthropic 格式**(`/anthropic` 端点,DeepSeek 2025-08 起支持)——仅在需要接入 Claude Code 生态时才有意义。

## 三、企业级主流架构参考(2026 行业调研)

### CodeRabbit(市场领导者)

```
入口层:  GitHub / GitLab / Azure DevOps / Bitbucket / CLI / IDE
   ↓
编排层:  review 引擎(触发 / 增量审查 / check 门禁 / 合规护栏)
   ↓
上下文工程: 代码图 + linter/SAST + 工单(Jira/Linear)
          + 监控(MCP: Sentry/Datadog) + 文档 + learnings + web search
          —— 全部先"蒸馏"(小模型提炼)再喂给审查模型
   ↓
模型层:  多模型路由 —— 小模型蒸馏 / 大模型深度推理 / judge 独立
```

核心实践:
- **混合 AI**:确定性分析(code graph / linter / SAST)+ agentic 推理,非纯 LLM
- **上下文工程**:每层上下文先蒸馏再喂入,审查模型只看到"与本次 PR 相关"的提炼结果
- **模型路由**:小模型蒸馏、大模型推理,judge 独立——与我们的 P1b 质量门思路一致
- **增量审查 + prompt caching**:未变更代码不重审,长 prompt 缓存(实测:无优化 DIY 审查 20 万 token/找到 1 bug,优化后 1.8 万 token,降 91%)
- **self-hosted**:容器化私有部署,支持开源模型(如 NVIDIA Nemotron)做蒸馏阶段
- **CodeRabbit Skills**:把审查能力打包给本地 agent 工作流

### Qodo Merge(原 PR-Agent, 开源)

- 多 agent 审查 + 规则执行;slash 命令(`/describe` `/improve` `/analyze` `/implement` `/compliance`)
- 开源引擎(pr-agent)可自托管,适合受监管行业
- **approve-or-tweak 工作流**:生成代码建议,审查者批准或微调(比 flag-and-fix 采纳率高)

### 行业共识(对我们的启示)

1. **MCP 的角色是"接外部上下文",不是"做 review"**——用 MCP 拉取 Sentry 报错、Datadog 监控、Confluence 需求,让审查从"代码是否合法"升级为"是否符合线上状态与业务目标";review 执行者始终是 LLM
2. **混合 AI 优于纯 LLM**:确定性工具(代码图/linter/SAST)做地基,LLM 做语义判断
3. **模型路由是标配**:不为所有任务用同一个大模型
4. **人机分工**:AI 处理 40-60% 机械审查(风格/bug/安全模式),人聚焦架构/设计/业务逻辑
5. **反馈三要素**:具体(定位到行)、可行动(给修复方向)、简洁(不埋在赘述里)——我们 prompt 规则 15"一语中的"与之吻合

## 四、目标架构(演进蓝图)

```
┌─ 入口层 ────────────────────────────────────────────────┐
│  GitHub Action │ GitLab │ CLI │ 本地/CI │ IDE(未来)       │
└───────────────┬─────────────────────────────────────────┘
                ▼
┌─ 编排层: review 引擎(平台无关纯函数) ────────────────────┐
│  触发/增量 → 上下文组装 → 审查 → 质量门 → 输出/门禁       │
│  (diff + 上下文 → issues, 不依赖任何平台的 API)          │
└───────┬────────────────────┬───────────────────────────┘
        ▼                    ▼
┌─ 上下文工程 ──────┐   ┌─ 确定性分析 ────────────┐
│ MCP 客户端(工单/   │   │ linter/SAST 交叉验证    │
│ 监控/文档/搜索)    │   │ 代码图(未来)           │
│ + 仓库规则/learnings│   │ 结构校验               │
└───────┬──────────┘   └───────┬───────────────┘
        ▼                    ▼
┌─ 模型层: 多模型路由(gateway 扩展) ─────────────────────┐
│  蒸馏(小模型, 上下文提炼) / 审查(大模型) / judge(独立)   │
│  私有化端点: vLLM / Ollama / DeepSeek / 任意兼容端点    │
└───────────────────────────────────────────────────────┘
```

## 五、演进路径(分阶段, 每阶段独立可交付)

| 阶段 | 内容 | 依赖 | 价值 |
|------|------|------|------|
| **1** | review 引擎"去 GitHub 化":把 pr_review 核心抽成"diff + 上下文 → issues"纯函数库,Action 只是入口之一 | 无 | 引擎可复用,CLI/本地入口成为可能 |
| **2** | 上下文工程:MCP 客户端接入外部上下文源(工单/监控/文档)+ linter 交叉验证层落地(P1 文档 lint 层,已设计未实现) | 1 | 审查从"代码合法"升级到"符合业务状态" |
| **3** | 模型路由:蒸馏小模型 / 审查大模型 / judge 独立;增量审查 + prompt caching | 1, 2 | 成本量级下降(参考 CodeRabbit 91%) |
| **4** | CLI + Skills 打包 + 私有化部署适配(容器化, 对接企业内网大模型) | 1, 2, 3 | 企业级落地形态 |

> 阶段 2/3 的部分组件已在 P1/P1b 文档中设计(质量门、lint 层、judge),实现时可复用。

## 六、设想评估表(CLI / skills / MCP / 私有化)

| 设想 | 评估 | 说明 |
|------|------|------|
| 自建 CLI | ✅ 合理 | CodeRabbit/Qodo 均有 CLI;作为本地/CI 入口与 Action 互补(阶段 4) |
| 能力中心下载 skills | ✅ 合理 | = CodeRabbit Skills 思路:审查能力打包成可复用 skill(阶段 4) |
| "调用 MCP 做 review" | ⚠️ 修正 | MCP 是**上下文源**(工单/监控/文档),review 执行者是 LLM;方向对但表述需修正(阶段 2) |
| 整合企业级私有化大模型 | ✅ 可行 | gateway 的 base_url 抽象已支持任意 OpenAI 兼容端点;审查链路无外部硬依赖 |

## 七、待确认项

- [ ] 阶段优先级:是否先做阶段 1(引擎去 GitHub 化)—— 成本最低、收益最广
- [ ] 上下文源优先级:先接哪个 MCP 源(工单 / 监控 / 文档)
- [ ] 模型路由的预算:是否值得引入小模型蒸馏(需要 benchmark 支撑,参考 golden 评测)
- [ ] 私有化部署的目标形态:容器化 self-hosted vs 纯 CLI 工具
- [ ] 是否保留"阶段 2/3 复用 P1 文档设计"的假设(quality-gate.md 的 lint 层、judge ground truth)

---

> 本文件为规划文档,不承载实现;实施时以本方案 + 各详设文档 + 当时代码为准。
