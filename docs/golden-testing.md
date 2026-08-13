# pr-review Golden 测试方案 — 质量回归基准

> 状态:**方案定稿, Phase 1 已实施**(2026-08-13)
> 关联: pr-review 质量保障 · 补齐测试体系最后一环(真实 e2e + 质量量化)
> 配套仓库: [Tania-X/ai-review-golden-tests](https://github.com/Tania-X/ai-review-golden-tests)

## 一、为什么需要

pr-review 是 LLM 应用,输出非确定性。现有测试(单测 mock + 闭环脚本)能保证**代码逻辑**正确,
但无法回答"审查质量好不好"——judge 打分准不准、会不会误报/漏报、评价是否恰当。

本方案用**一组"标准答案已知"的代码变更场景**,让 pr-review 在真实 GitHub 环境里逐个审查,
比对"实际输出 vs 期望输出",得到**可重复、可量化的质量报告**。

## 二、架构

```
golden 场景(已知 bug / 干净代码 / 文档变更 / 误报诱饵)
        │ 驱动器: 建分支 → 变更 → push → 开 PR
        ▼ (pr-review 自动跑, 异步)
轮询 check-run / 评论 → 解析 issues
        ▼ 断言 expected.json vs 实际
量化报告: 精确率 / 召回率 / judge 合理性 / 场景矩阵
```

## 三、场景矩阵(golden 场景)

| 场景 | 变更 | 期望(核心断言) |
|------|------|----------------|
| `case-bug` | nil 指针解引用 | 报 error |
| `case-security` | SQL 拼接 + 硬编码密钥 | 报 security |
| `case-convention` | 吞掉 error(违反 AGENTS.md) | 报 convention/约定类 |
| `case-clean` | 干净重构 | **0 issues + 正面评价** |
| `case-docs` | 纯文档变更 | 0 issues + 质量门 pass(短路) |
| `case-bait` | 看起来像问题、实为设计意图 | **不报确定性 error**(可标 needs_review) |

## 四、量化指标

- **精确率 precision** = 真命中数 / 全部报出数 —— 反映"误报"水平
- **召回率 recall** = 真问题被报出数 / 应有真问题数 —— 反映"漏报"水平
- **误报** = `case-clean`/`case-bait` 不该报却报了
- **judge 合理性** = 干净/文档场景应 pass、有 bug 场景不应被误降级

## 五、实施阶段

### Phase 1(已完成) — 场景定义 + 期望

- 测试仓库已建,含 6 个场景(每场景 `changes/` + `expected.json`)
- 接入 pr-review workflow(@main, 质量门开启, reopened 已补)
- 可手工逐场景开 PR 观察表现、校准 expected

### Phase 2(规划) — 自动化驱动器

`run_golden_tests.py`(放 ai-tools `golden-tests/` 或测试仓库):

```
逐场景: git 建分支 → cp changes/ → push → gh/git 开 PR
  → 轮询 check-run 完成(15s 间隔, 超时 5min)
  → 拉评论 + check-run → 解析 issues(按评论格式提取)
  → 断言 expected vs actual → 关 PR
→ 汇总报告(精确率/召回率/场景矩阵) + 退出码
```

关键点:
- **异步时序**: 用 GitHubClient 轮询 check-run / review 评论(pr-review 跑完需 1-2 分钟)
- **非确定性**: 断言用"级别/分类"而非"精确文案"; 必要时单场景跑 2-3 次
- **成本**: 全量 6 场景 × (审查+质量门) ≈ 几十万 token,DeepSeek 几分钱; 调试可 `--dry-run`

## 六、使用方式(每次改 prompt 后)

```bash
# 改完 pr-review 的 prompt/打分逻辑后:
python run_golden_tests.py            # 跑全量, 出精确率/召回率报告
# 对比上次报告 → 判断质量提升/退化
```

## 七、待确认项

- [ ] 驱动器脚本放 ai-tools 还是 golden 测试仓库(建议 ai-tools,与代码同仓)
- [ ] 是否需要"每次 PR 自动跑 golden"作为 pr-review 自身的 CI(暂不需要,手动按需跑即可)
- [ ] `case-bait` 的判定口径: `needs_review` 提示算不算"误报"(建议不算,但单独统计)
