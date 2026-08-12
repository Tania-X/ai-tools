# pr-review 质量闭环路线图(P1/P2 合并设计)

> 状态:**已设计,未实现**(2026-08-12 合并定稿)
> 来源:三个独立想法的统一——① 线程决议驱动(反馈侧) ② 质量打分与自检重写(输出侧)
> ③ PR 描述自动补全(输入侧)。原详设见:
> - [pr-review-quality-gate.md](pr-review-quality-gate.md)(② 输出侧,含低成本校验层)
> - [pr-review-desc-autofill.md](pr-review-desc-autofill.md)(③ 输入侧)
> - 本文件新增 ① 反馈侧方案,并将三者统一到"审查质量闭环"框架

---

## 一、统一视角:从"一次性输出"到"质量闭环"

pr-review 当前是一次性链路:PR 事件 → 审查 → 发评论 + check-run。升级目标:

```
        输入侧                       输出侧                        反馈侧
┌────────────────────┐   ┌─────────────────────┐   ┌──────────────────────┐
│ ③ desc 自动补全     │   │ ② 质量门              │   │ ① 线程决议驱动         │
│ PR 描述完整 →       │──▶│ judge 打分自检 →      │──▶│ 用户在线程表态 →       │
│ 审查/人更好理解改动  │   │ 质量可靠才发布         │   │ 下轮不再重复报          │
└────────────────────┘   └─────────────────────┘   └──────────────────────┘
        让"输入"可信                让"输出"可靠                让"反馈"被记住
```

三个子能力解决不同侧的问题,且**数据流互相衔接**(见第三节),不是孤立功能。

## 二、子方案概要

### ① 反馈侧:线程决议驱动(本文件新增)

**目标**:审查"记得"讨论结论——用户在线程里说"这是设计意图/已解决"后,下轮不再重复报。

**机制**(建立在 P0 线程交互之上,零新增存储,thread 即状态):

1. 用户在线程回复 → P0 的 ReplyHandler 已能对话;P1 在生成回复的同时**解析用户意图**:
   - `resolve`(已解决)/ `ignore`(设计意图,忽略)/ `ask`(仅提问,不改变状态)
   - 判定方式:规则关键词(如"已解决""设计意图""忽略""不用改")+ LLM 意图识别兜底
2. 决议以"AI 回复内容中的标记"沉淀在对话链上(如回复末尾附 `<!-- resolve -->`),
   或直接以线程结构推断——**不引入数据库**
3. 下轮 review 运行时扫描该 PR 全部 AI 评论线程,提取被 `resolve/ignore` 的 (file, line) 集合
   → 注入 prompt 的"已处理清单":"以下问题已在对话中确认解决或属设计意图,不要重复报"
4. 效果:噪音维度显著下降,误报被"就地消灭",无需等下一轮才发现

**衔接**:已处理清单同时喂给 judge(②),重复报已忽略问题 = 噪音扣分;重写时也带清单。

### ② 输出侧:质量门(LLM-as-judge 自检循环)

按 [pr-review-quality-gate.md](pr-review-quality-gate.md) 原设计纳入,要点:

- 审查产出 → **独立 judge LLM** 打分(rubric: 准确性/可操作性/覆盖度/噪音)
- `score < pass_score(70)` → 带 reasons **整批重审**(Reflexion 式,重写不叠加 issues)
- 硬上限 `max_rewrites(3)`;超限降级:不发低质量评论,发说明评论 + check-run **neutral**
- **低成本校验层**:结构校验(零成本,行号 ∈ diff / severity 合法)+ linter 交叉验证
  (秒级,只作 judge 参考信号,不直接否决——避免 LLM 变 linter 复读机)

### ③ 输入侧:PR 描述自动补全

按 [pr-review-desc-autofill.md](pr-review-desc-autofill.md) 原设计纳入,要点:

- `desc` 模式(与 review/reply 并列),监听 `pull_request opened`
- 复用现有组件(diff + ContextCollector + LLM),新增一次 `GET /pulls/{n}/commits` 调用
- 生成 {title, body} → PATCH 更新 PR
- **覆盖策略保护手写**:body 为空或 <20 字才生成;title 为空或默认标题才生成;更新后发提示评论

## 三、衔接矩阵(三者如何协同)

| 衔接 | 说明 |
|------|------|
| 决议清单 → 审查者 | 下轮审查 prompt 注入"已处理清单",不重复报(①→review) |
| 决议清单 → judge | 重复报已忽略问题 = 噪音维度扣分(①→②) |
| 决议清单 → 重写 | 重写时携带清单,重写轮不复活已忽略问题(①→②) |
| desc → judge | judge 的"覆盖度"评估参考 PR 描述声称的改动范围 vs 实际审查覆盖(③→②) |
| desc → 审查者 | 上下文收集已含 README/AGENTS;desc 补全后 PR 信息更完整(③→review 间接) |

## 四、实施顺序(建议,每步独立可交付)

| 阶段 | 内容 | 改动面 | 前置 |
|------|------|--------|------|
| P1a | ① 线程决议驱动 | ReplyHandler 解析意图 + review 扫描线程注入清单 | P0(已完成) |
| P1b | ② 质量门 | 新增 `pr-review/pr_review/quality.py`(judge+重写循环+校验层) | 无 |
| P2 | ③ desc 自动补全 | main.py 加 desc 模式 + desc.py | 无 |

> P1a 最小且直接建立在 P0 上,建议先做;P1b 独立模块可并行;P2 最独立,排最后。

## 五、配置草案汇总(.ai-review.yaml 增量)

```yaml
# ① 反馈侧
resolve:
  enabled: true            # 扫描线程提取已处理清单
  max_handled_lines: 200   # 清单注入上限

# ② 输出侧(详设见 quality-gate.md)
quality_gate:
  enabled: true
  judge_provider: deepseek
  judge_model: deepseek-chat
  pass_score: 70
  max_rewrites: 3
  max_judge_input_chars: 8000
  lint:
    enabled: true
    only: [py, js, go]

# ③ 输入侧(详设见 desc-autofill.md)
desc_autofill:
  enabled: true
  min_body_chars: 20       # body 短于此才生成
```

## 六、待确认项(实现前用户拍板)

- [ ] 实施顺序:P1a(P1b) → P2,是否同意先做 P1a
- [ ] 决议判定:关键词规则 + LLM 兜底 vs 纯规则(简单但漏判)
- [ ] `pass_score: 70` / `max_rewrites: 3` 默认值
- [ ] 质量门降级时是否附带最后一次 issues 摘要(而非完全不发)
- [ ] desc 自动补全是否需要支持 PR template(.github/PULL_REQUEST_TEMPLATE.md)

---

> 本文件为规划文档;实现时以本方案 + 各详设文档 + 当时代码为准。
