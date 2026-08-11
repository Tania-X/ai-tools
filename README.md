# ai-tools — AI × DevOps 工具集

独立的 AI 能力层仓库,服务包括 [devops-dashboard](https://gitee.com/Max_1996/devops-dashboard) 在内的项目。

## 模块

| 模块 | 说明 | 状态 |
|------|------|------|
| `gateway` | LLM 统一网关(DeepSeek/Kimi/OpenAI 兼容,多 key 轮询/重试/成本统计) | ✅ 骨架完成 |
| `pr-review` | GitHub Action AI PR 审查(自建 CodeRabbit 替代) | 🔧 开发中(feat/pr-review) |
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
3. 打开 PR 即可看到审查评论(整体评论,JSON 结构化,含文件+行号)

## 文档

- [设计文档](docs/design.md) — 架构、模块规划、技术选型、学习路径
