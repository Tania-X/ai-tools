# ci-diagnose 使用指南 — CI 失败自动诊断

> CI job 失败时, AI 自动分析失败日志, 评论到 PR: 根因 + 定位 + 修复建议。
>
> 更新: 2026-08-12

## 一、原理

独立 composite action,监听 `workflow_run` 事件(目标 CI workflow 完成时触发):

```
目标 CI workflow 完成(failure)
        │ workflow_run completed 事件
        ▼
ci-diagnose 运行
  ├─ 只处理 failure / 非 fork / 能找到对应 PR 的 run
  ├─ 拉取失败 job 的日志(zip 解压容错)
  ├─ 日志截断: 错误关键字行(±2) + 尾部 30 行, 预算 max-log-chars(默认 8000)
  ├─ LLM 诊断 → {summary, root_cause, location, suggestion, fix}
  └─ 评论到 PR(issue comments)
```

## 二、接入步骤

在目标仓库(如 devops-dashboard)main 分支放 `.github/workflows/ci-diagnose.yml`:

```yaml
name: CI Diagnose

on:
  workflow_run:
    workflows: ["CI"]        # 目标 CI workflow 的 name(按需改)
    types: [completed]

permissions:
  contents: read
  actions: read              # 读取 workflow 日志必需
  pull-requests: write       # 评论 PR 必需

jobs:
  diagnose:
    runs-on: ubuntu-latest
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    steps:
      - uses: Tania-X/ai-tools/ci-diagnose@main
        with:
          api-key: ${{ secrets.DEEPSEEK_API_KEY }}
          # model: deepseek-chat      # 可选
          # max-log-chars: 8000       # 可选, 日志注入预算
```

注意:
- `workflow_run` 事件**只监听同一仓库**的 workflow(不跨仓库、默认不触发自身)
- 目标 CI workflow 的 `name` 必须与 `workflows:` 里写的一致
- 依赖 Secret: `DEEPSEEK_API_KEY`(与 pr-review 同一个即可)

## 三、行为说明

- **只诊断失败**:conclusion != failure 直接跳过
- **fork 不诊断**:head_repository 不是本仓库时跳过(防污染)
- **只评论 PR**:head_sha 找不到开放 PR(如 push 到 main 的 CI)时跳过
- **日志聚焦**:错误关键字行 + 尾部,控制 token;信息不足时 LLM 会明说"日志片段不足",不瞎猜
- **评论格式**:结论 / 失败位置 / 根因 / 修复建议 / 修复示意(可选) / 日志链接

## 四、常见问题

| 现象 | 原因 / 解法 |
|------|------------|
| 诊断不触发 | `workflows:` 里的名字与目标 workflow `name` 不一致; 或目标 workflow 与诊断 workflow 不在同一仓库 |
| 报权限错误 | 缺 `actions: read`(拉日志)或 `pull-requests: write`(评论) |
| 评论到错误的 PR | `workflow_run` 按 head_sha 匹配开放 PR; 多 PR 共用同一 commit 时取第一个匹配 |
| 日志太长 token 超 | 调小 `max-log-chars`; 或日志本身超大时考虑只看失败 job(实现已按 job 拉取) |

## 五、与 pr-review 的关系

- 独立 action,独立触发事件,互不依赖;gateway(LLM 配置)复用同一套
- 定位: pr-review 管"代码审查",ci-diagnose 管"CI 故障"; 合起来覆盖开发闭环
