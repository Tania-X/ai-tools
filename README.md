# ai-tools — AI × DevOps 工具集

独立的 AI 能力层仓库,服务包括 [devops-dashboard](https://gitee.com/Max_1996/devops-dashboard) 在内的项目。

## 模块

| 模块 | 说明 | 状态 |
|------|------|------|
| `gateway` | LLM 统一网关(DeepSeek/Kimi/OpenAI 兼容,多 key 轮询/重试/成本统计) | ✅ 骨架完成 |
| `pr-review` | GitHub Action AI PR 审查(自建 CodeRabbit 替代) | ✅ 可用(已合并 main,接入 devops-dashboard) |
| `alert-explain` | 告警解读/根因建议 | 规划 |
| `log-analyzer` | 异常日志分析 | 规划 |
| `ops-query` | 自然语言查询指标/状态 | 规划 |
| `mcp-server` | 统一暴露给 AI 助手(MCP) | 规划 |

## 快速开始(pr-review)

在任意仓库配置一个 workflow,监听 `pull_request` 事件:

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
        with:
          api-key: ${{ secrets.DEEPSEEK_API_KEY }}
          model: deepseek-chat
```

首次使用:

1. 仓库根放 `.ai-review.yaml`(可复制 [pr-review/.ai-review.yaml.example](pr-review/.ai-review.yaml.example))
2. 配置一个 OpenAI 兼容 API key 的 Secret
3. 打开 PR 即可看到审查结果:
   - **整体评论**: summary + 问题清单 + token/成本统计
   - **行内评论线程**: 每个可定位的问题挂在 diff 对应行上, 可直接在行上回复讨论
   - **check-run 门禁**: 存在达到 `fail_on_severity`(默认 error)级别的问题时 PR 变红,
     配合分支保护规则可阻止合并; AI 不确定(needs_review)的问题不计入门禁

## 接入其他仓库(Checklist)

以 devops-dashboard 为参考,给任意 GitHub 仓库接入的完整步骤:

1. **workflow 文件必须合入 main** — `pull_request` 事件读取 base 分支(main)的 workflow 定义,文件不在 main 上不会触发
2. `.github/workflows/ai-review.yml` 内容(注意 `permissions.pull-requests: write` 是发评论必需的):

   ```yaml
   name: AI Review
   on:
     pull_request:
       branches: [main]
       types: [opened, synchronize]
   permissions:
     contents: read
     pull-requests: write
   jobs:
     ai-review:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: Tania-X/ai-tools/pr-review@main
           with:
             api-key: ${{ secrets.DEEPSEEK_API_KEY }}
   ```

3. 仓库 Settings → Secrets and variables → Actions 添加 `DEEPSEEK_API_KEY`(或改成自己的 secret 名)
4. 仓库根放 `.ai-review.yaml` 控制噪音(`min_severity` 默认 warn,嫌吵调 error)

> 提示: 若开发流是 Gitee/其他平台 + GitHub 镜像, 只需把带 workflow 的 main 同步到 GitHub 镜像即可,
> 审查只发生在 GitHub 侧, 源平台无需任何改动。

## 文档

- [设计文档](docs/design.md) — 架构、模块规划、技术选型、学习路径
- [pr-review 使用指南](docs/pr-review.md) — 实现原理、前提条件、手动配置流程、配置项参考
