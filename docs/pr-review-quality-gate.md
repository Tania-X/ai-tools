# pr-review 质量打分与自检重写(P1 规划)

> 状态:**已设计,未实现**(方案 2026-08-11 讨论定稿)
> 关联:pr-review 增强 · 对应 CodeRabbit 类产品的"质量评估/自检"能力
> 定位:解决 LLM 审查输出质量不稳定(误报/漏报/幻觉/建议空泛)的兜底机制

---

## 一、背景与动机

pr-review 已具备完整审查链路(整体评论 + 行内线程 + check-run 门禁 + P0 线程交互),
但 LLM 输出质量本质上有波动:

- **误报**:无中生有或过度挑剔的问题(经 v2 改进已下降,但无法根除)
- **漏报**:真正的问题没被审出来,或建议空泛不具可操作性
- **幻觉**:引用了不存在的代码行 / 契约条款

v2 的降误报是从"审查输入"侧(仓库上下文、生成代码排除)改善;**本方案从"审查输出"侧
兜底**——审查完成后引入独立的 judge LLM 打分,不达标自动带反馈重写,仍不达标则明确告知
用户,避免低质量审查悄悄污染 PR。

## 二、方案概览:LLM-as-a-judge 自检循环

```
LLM 审查产出 issues(JSON)
        │
        ▼
Judge LLM 打分(独立 prompt / 独立模型,可选)
  rubric: 准确性 / 可操作性 / 覆盖度 / 噪音
        │
        ▼
score ≥ pass_score ? ──是──▶ 发布评论 + check-run 门禁
        │否
        ▼
重写次数 < max_rewrites ? ──是──▶ 带 judge 的 reasons 整批重审 ──▶ 回到打分
        │否
        ▼
降级提示:不发低质量评论,提醒手动 rerun 或人工 review;
check-run 置 neutral(不阻塞合并)
```

## 三、设计决策

### 3.1 judge 独立于审查者(核心)

用**第二个 LLM(独立 prompt,模型可独立配置)**当质检员,不让审查 LLM 自评——
自评存在"自我感觉良好"偏差。

judge 输入:

1. 审查产出的 issues 清单(JSON)
2. 对应文件 diff(用于核对准确性)
3. 评分标准(rubric)

judge 输出(JSON):

```json
{
  "score": 0-100,
  "verdict": "pass" | "rewrite",
  "reasons": ["逐条说明扣分点(供重写时作为反馈)"]
}
```

### 3.2 打分粒度:整个 PR 审完打一次分

- 不按批次打分:判断"覆盖度 / 噪音"需要全局视角,分批打分成本翻倍且批次间上下文割裂
- 大 PR(多批次)时,judge 输入按需截断(参考 `max_context_chars` 思路,设 `max_judge_input_chars`)

### 3.3 重写策略:带 reasons 整批重审(Reflexion 式)

- judge 的 reasons(如"第 3 条是误报""缺少对 X 的检查""建议不具体")作为反馈注入重写 prompt
- **整批重审而非"只修指出的问题"**:漏报必须重看全部 diff 才能补
- 重写轮次之间的 issues 不叠加,以最后一轮为准

### 3.4 阈值与重试上限

| 参数 | 建议默认 | 说明 |
|------|---------|------|
| `pass_score` | 70 | 低于则触发重写;太低失去门禁意义,太高重试爆炸 |
| `max_rewrites` | 3 | 硬上限,防死循环;每次重写前记录 attempts,达到即降级 |

### 3.5 三次失败降级(用户可见行为)

- **不发布低质量评论**(避免污染 PR 讨论区)
- 发布一条说明性评论,文案示例:

  > ⚠️ 本轮 AI 审查质量评估未达标(3 次重写均低于阈值,最后一次评分 X/100)。
  > 建议:① 稍后手动 rerun 本 workflow;② 或人工 review。

- check-run 置 **neutral**(非 success 也非 failure):"审查质量差"不是代码缺陷,不阻塞合并

## 四、评分维度(rubric)

| 维度 | 考察点 | judge 判断依据 |
|------|--------|----------------|
| 准确性 | issue 是否真对应代码问题(幻觉/误报) | issues 中的行号/引用与 diff 的对应关系 |
| 可操作性 | 修改建议是否具体可执行 | suggestion 是否包含明确的改动方向/代码示意 |
| 覆盖度 | 关键改动是否被审到(漏报) | 新增/删除的核心逻辑是否有对应 issue 或 summary 覆盖 |
| 噪音 | 是否过度挑剔无关紧要的 | warn/info 级问题是否聚焦于真实风险 |

### 4.1 低成本校验层(judge 的辅助输入)

> 思路:质量打分不必全交给 LLM——**确定性内容用确定性工具**,LLM 只判断语义问题。

**两层校验,成本递增:**

| 层 | 内容 | 成本 | 说明 |
|----|------|------|------|
| 结构校验 | JSON schema、行号 ∈ diff 新增行、severity 合法、file 存在 | 零(纯代码) | pr-review 已有部分,固化进 judge 前置校验 |
| linter 交叉验证 | 对 PR 涉及文件跑对应语言 linter,产出确定性错误清单,作为 judge 输入之一 | 低(秒级,无 token) | 见下 |

**linter 结果的三种用法(作为信号,不直接否决):**

1. LLM issue **命中** linter 也报的错误 → 高置信,加分
2. LLM issue 是 linter **未报**的语义级问题 → 保持 LLM 判断(linter 只覆盖语法/局部规则,不覆盖语义,不因"linter 没报"就否决)
3. LLM issue 与 linter 冲突(如行号根本不存在)→ 疑似幻觉,扣分

**关键原则:linter 结果只作 judge 的参考信号,不直接否决 LLM 输出**——否则 LLM 变成 linter 的复读机,且 linter 自身误报会反向污染审查结果。

**配置草案(quality_gate 下):**

```yaml
quality_gate:
  ...
  lint:
    enabled: true            # 关闭则完全跳过本层
    only: [py, js, go]       # 只对已接入的语言跑,其他语言跳过
    # 语言 → 命令由实现侧维护;linter 版本需锁定(pin),避免输出格式漂移
```

## 五、成本分析

最坏情况 = 1 次原审 + 3 次重写 + 4 次 judge ≈ **4 倍审查成本**。

- judge 输入 = 审查输出 + diff 摘要,远小于审查本身的输入,单次成本占比低
- DeepSeek/Kimi 价格下,单 PR 全量审约几分钱,4 倍仍在可接受范围
- 保留 `quality_gate.enabled` 开关,异常时可一键关闭

## 六、与现有代码的接缝(实现时参考,侵入性小)

改动集中在两处:

1. `ReviewRunner.run()` 末尾:result 产出后先过 judge;不通过则带 reasons 重跑 `_review_batch`(整批),重复至 `max_rewrites`
2. `main.py` 发布前:根据最终 verdict 分流——通过走正常发布;降级走说明评论 + neutral check-run

新增模块建议:`pr-review/pr_review/quality.py`(Judge 评分 + 重写循环 + rubric prompt)

## 七、配置项草案(.ai-review.yaml)

```yaml
quality_gate:
  enabled: true        # 一键开关
  judge_provider: deepseek   # 可选,默认同审查 provider
  judge_model: deepseek-chat # judge 用模型(可独立于审查模型)
  pass_score: 70       # 低于则触发重写
  max_rewrites: 3      # 重写硬上限
  max_judge_input_chars: 8000  # judge 输入预算,大 PR 截断用
```

## 八、待确认项(实现前需要用户拍板)

- [ ] judge 模型:同模型换 prompt(默认)vs 独立更强模型 —— 已倾向同模型,可配置
- [ ] `pass_score: 70` 默认值是否合适
- [ ] 三次失败后的降级文案与 check-run neutral(不拦合并)是否符合预期
- [ ] 是否需要在降级时附带最后一次的 issues 摘要给用户参考(而非完全不发)

---

> 本文件为规划文档,不承载实现;实现时以本方案 + 当时代码为准。
