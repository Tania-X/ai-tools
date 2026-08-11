# pr-review PR 描述自动补全(P2 规划)

> 状态:**已设计,未实现**(方案 2026-08-11 讨论定稿)
> 关联:pr-review 增强 · 创建 PR 后由 AI 自动生成并更新 title / description
> 定位:解决 PR 描述缺失或质量差的问题,提高 PR 可读性与评审效率

---

## 一、背景与动机

开发者创建 PR 时常省略或草草写描述,导致 reviewer 难以快速理解改动意图。
本功能在 PR 创建后由 AI 根据提交内容自动生成 title + description 并更新到 PR,
让 PR 界面始终有一份结构化的变更说明。

**现实约束(先说清楚)**:GitHub 的 PR 创建页(compare 界面)是官方 UI,
外部工具**无法向该页面的输入框注入内容**——"打开创建页时已预填"只能靠浏览器插件
(见第四章,不推荐)。本方案采用 GitHub Action **"创建后自动补全"**:
创建 PR 时可不填,Action 在几十秒内用 API 补上,效果接近预填且零额外安装。

## 二、方案概览:创建后自动补全

```
① 开发者 push 分支 → GitHub 出现 "Compare & pull request"
② 创建 PR(title/description 可先留空)→ 触发 pull_request opened 事件
③ Action 运行 desc 模式:复用现有 diff 获取 + 仓库上下文(ContextCollector),
   新增获取 commits 列表(一次 GitHub API 调用)
④ AI 生成 {title: 一句话总结, body: 分节描述}
⑤ 覆盖策略校验通过后,PATCH /repos/{owner}/{repo}/pulls/{number} 更新 PR
   (仅当用户未手写描述时覆盖,保护手写内容)
⑥ 完成:PR 界面出现完整描述;可选发一条简短评论告知"已自动补全"
```

## 三、设计决策

### 3.1 触发与运行模式

- 监听 `pull_request` 的 `opened` 事件(与现有审查同一类事件)
- 在 `main.py` 的模式分派中新增 `desc` 模式(与现有 `review` / `reply` 并列,
  按 `GITHUB_EVENT_NAME` + PR 状态区分)
- 时序:先补描述(几十秒),审查照常跑,**互不阻塞**

### 3.2 生成内容与输入(全部复用现成组件)

| 输入 | 来源 | 状态 |
|------|------|------|
| PR 文件 diff | `GitHubClient.get_pr_files()` | 已有 |
| 仓库上下文(约定/契约) | `ContextCollector.collect()` | 已有 |
| commits 列表(消息摘要) | GitHub API `GET /pulls/{n}/commits` | 新增一次调用 |
| LLM 调用 | `gateway.LLMClient` | 已有 |

输出(JSON):

```json
{
  "title": "feat: 一句话总结本次变更",
  "body": "## 变更概述\n...\n## 主要改动\n- ...\n## 测试\n- ...\n## 风险/注意\n- ...\n"
}
```

body 分节建议:变更概述 → 主要改动点(按文件/模块)→ 测试情况 → 风险/注意 → 关联 issue(如有)。

### 3.3 覆盖策略(核心,必须保护用户手写)

**绝不能无脑覆盖用户已写的内容。** 规则建议:

1. `body` 为空**或极短**(< 20 字)时才生成 body
2. `title` 为空**或为 GitHub 默认标题**(如 "Update README.md")时才生成 title
3. 更新后发一条简短评论:"🤖 已自动补全 PR 描述,点击编辑可修改"——让用户有感知、可回退

### 3.4 PR template 支持(可选,排后)

仓库存在 `.github/PULL_REQUEST_TEMPLATE.md` 时,把模板结构喂给 AI 按模板填写,
输出与团队约定一致。解析模板需额外工作量,优先级低于 3.1-3.3。

## 四、备选方案(不推荐)

**浏览器插件预填**(真正"打开页面就有"):Tampermonkey 用户脚本 + 本地服务,
在打开 compare 页面时注入 title/body。代价:需安装插件、换机器要重装、
脚本与 GitHub 页面结构耦合需持续维护——与个人项目"零部署"定位冲突。

## 五、成本

- 一次额外 LLM 调用(输入 = diff 摘要 + commits,输出 = title + body)
- **比审查便宜一个量级**:无批次、无重试循环;复用 gateway 的多 key / 重试 / 成本统计
- 大 PR 时 diff 按 `max_context_chars` 思路截断

## 六、与现有代码的接缝(实现时参考,侵入性小)

1. `main.py`:模式分派新增 `desc`(opened 且触发)分支
2. 新增 `pr-review/pr_review/describe.py`:生成 title/body + 覆盖策略校验 + PATCH 更新
3. `GitHubClient`:新增 `get_pull_commits()` 与 `update_pull(title, body)`
4. 覆盖策略校验放在 PATCH 前,确保零误覆盖

## 七、配置项草案(.ai-review.yaml)

```yaml
desc_autofill:
  enabled: true          # 一键开关
  min_body_chars: 20     # body 短于此视为"未写",触发补全
  title_convention: conventional  # conventional | plain
  notify_comment: true   # 更新后发评论告知用户
  use_template: false    # 是否按 PULL_REQUEST_TEMPLATE.md 填写(可选)
```

## 八、待确认项(实现前需要用户拍板)

- [ ] 覆盖策略:"body 为空或 <20 字才生成"是否认可,还是"总是生成、追加到 body 末尾"
- [ ] title 风格:conventional commits 前缀(`feat:`/`fix:`),与现有 commit 风格一致?
- [ ] 是否支持 PR template(默认不做)

---

> 本文件为规划文档,不承载实现;实现时以本方案 + 当时代码为准。
