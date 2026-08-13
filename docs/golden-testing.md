# pr-review Golden 测试方法论

> 状态:**方法论定稿**(2026-08-13),自动化待网络稳定后实施
> 关联: pr-review 质量保障 · 配套仓库 [Tania-X/ai-review-golden-tests](https://github.com/Tania-X/ai-review-golden-tests)
> 定位: 用"已知答案"的场景,在真实 GitHub 环境里量化 pr-review 的审查质量,并验证"审查驱动的修复闭环"是否自洽

## 一、目标

pr-review 是 LLM 应用,输出非确定性。单测(mock)只能保证代码逻辑正确,无法回答质量好坏。
本方法论用**标准答案已知的场景**,让 pr-review 在真实环境逐个审查,得到可重复、可量化的质量报告。

两个层次:
1. **Level 0 快照评测** —— 单次审查能否正确发现问题/不误报(质量本身)
2. **Level 1 闭环评测** —— "review 拒绝 → 修复 → review 放行 → 合并"整条链是否自洽(协作闭环)

## 二、核心概念

### case 三要素

一个 case = **场景代码(自然提交) + 阶段快照 + 契约(期望)**:

```
scenarios/<case>/
├── changes/          # buggy 版(缺陷快照, Level 0 与 Level 1 的第一阶段)
├── fixed/            # 修复版(Level 1 的第二阶段; 正样本才有)
└── expected.json     # 契约(期望断言 + lifecycle 标记)
```

- `changes/` 是"引入缺陷"的提交内容(代码不剧透,见防剧透原则)
- `fixed/` 是"按审查意见修复后"的提交内容(对应"fix according to review")
- `expected.json` 是断言基准,只描述"期望",不含答案(答案在 `docs/golden-contract.md`)

### case 分类(决定 Level 适用性)

| 类别 | case | 审查应拒绝? | Level 适用 |
|------|------|------------|-----------|
| **正样本** | bug / security / convention | 应 refuse(报 error) | Level 0 + Level 1 |
| **负样本** | clean / docs | 应直接 agree(无问题) | 仅 Level 0 |
| **边界样本** | bait | 应 agree(可给 warn/info 建议, 不报 error) | 仅 Level 0 |

**自洽逻辑**: 正样本才有"refuse→fix→agree"闭环; 负/边界样本没有 buggy→fixed 的转换,
只验证"单次审查能否正确放行"。

## 三、测试层级

### Level 0 — 快照评测(单次审查质量)

**测什么**: 一次审查能否正确发现缺陷、不误报、judge 打分合理。

**流程**(逐 case 循环):

```
1. git checkout main → 建分支 test/<case>
2. 应用 changes/ → commit → push
3. 开 PR
4. 轮询 review 完成(check-run 终态 / 评论出现, 15s 间隔, 超时 5min)
5. 拉取 review 结果 → 解析 issues(severity/category/line)
6. 断言 expected vs actual → 存本地 results/<case>.json
7. 关 PR + 删分支
8. 下一个 case, 直到全部测完
```

**指标**:
- 精确率 precision = 真命中 / 全部报出(反映误报)
- 召回率 recall = 真问题被报出 / 应有真问题(反映漏报)
- 误报 = clean/docs 报出任何问题、bait 报 error
- judge 合理性 = 负样本应 pass、正样本不应被误降级

### Level 1 — 闭环评测(审查驱动的修复闭环)

**测什么**: "review 拒绝 → 按意见修复 → review 放行 → 合并"整条链是否自洽。
即: 该拒的拒了、修复后该放的放了、最终能 merge。

**流程**(正样本, 在 Level 0 基础上延续):

```
1~5. 同 Level 0(buggy 版 → review → 期望 refuse)
6. 应用 fixed/ → commit → push(同一 PR, 触发 synchronize)
7. 轮询 review #2
8. 断言: 修复后应 agree(无 error / check-run 通过)
9. gh pr merge → 删分支
```

**指标**:
- refuse 正确率 = 正样本第一阶段被拒的比例
- agree 正确率 = 正样本修复后放行的比例
- 假拒绝 = 负样本被拒(应在 Level 0 就发现)

## 四、Level 1 状态机

```
正样本 case:
  buggy ──push──▶ review#1 ──期望 refuse──▶ fixed ──push──▶ review#2 ──期望 agree──▶ merged

负/边界样本 case:
  (无 buggy/fixed 转换) ──push──▶ review ──期望 agree──▶ merged
```

"fix according to review" 用**预置的 fixed/ 快照**而非让 AI 修复——保证确定性、可重复,
避免把"AI 修复质量"这个额外变量混进测试。

## 五、驱动器接口(定义, 不实现)

`run_golden_tests.py`(计划放 ai-tools `golden-tests/`):

```
python run_golden_tests.py [--level 0|1] [--cases case-bug,case-security] [--dry-run]

逐 case:
  Level 0: build-branch → apply(changes) → push → open-pr → poll → fetch → assert → store → close
  Level 1: 上述 + apply(fixed) → push → poll#2 → assert-agree → merge

输出:
  results/<case>-<timestamp>.json   每个 case 的原始 review 结果(本地留存)
  report.md                          汇总: 场景矩阵 + 精确率/召回率 + 失败详情
  退出码: 有 fail → 非零(供 CI 判断)
```

关键实现点:
- **异步时序**: 复用 GitHubClient 轮询 check-run / review 评论(pr-review 跑完 1-2 分钟)
- **结果解析**: 从评论 Markdown 提取 issues(severity/category/line),断言用"级别/分类"而非精确文案
- **成本**: 全量 6 case × (审查+质量门) ≈ 几十万 token,DeepSeek 几分钱

## 六、结果存储与历史基线

- 每次运行把每个 case 的**原始 review 结果**(issues 列表 + judge 评分 + check-run 结论)存到
  `results/<case>-<ts>.json`,本地留存(用户可离线复查)
- `report.md` 是汇总入口,可与历史报告对比看**质量趋势**(每次改 prompt 后跑一遍)
- 报告是"证据链": 从"LLM 说了什么"到"判定为什么 pass/fail"全程可追溯

## 七、自我演进机制

方法本身随使用持续演进:

1. **case 注册表**: `scenarios/manifest.json` 列出所有 case + 分类 + Level 适用性;
   驱动器读 manifest 决定跑哪些、跑几层
2. **新增 case 三步骤**: 写场景(自然提交) → 写契约(expected.json + golden-contract.md) → manifest 注册
3. **缺陷复杂度分层**: 当前覆盖简单、无歧义缺陷; 后续补充复杂缺陷
   (并发竞态、跨函数契约破坏、边界条件),用 manifest 标记难度
4. **期望演进纪律**(关键, 防"自欺"): 结果与期望不符时, 区分两种情况——
   - **AI 退化**(本该报没报 / 误报增加)→ 改 pr-review 的 prompt/逻辑
   - **期望过时**(契约本身写错 / 场景设计有歧义)→ 更新契约, 但**必须人工评审**,
     防止"为了通过而放宽期望"
5. **结果基线沉淀**: `results/` 历史对比, 让"质量在提升还是退化"有据可查

## 八、逻辑自洽判定规则

- 每个 case **独立判定** pass/fail, 互不影响; 失败不阻塞其他 case
- pass 标准 = expected.json 全部断言满足; 任一不满足 = fail(记录具体差异)
- 报告结论只取三种: pass / fail / skip(网络异常等, 不误判为 fail)
- 一套运行的最终结论 = 各 case 结果的聚合, 不搞"模糊加权"

## 九、网络依赖与实施约束

- **强依赖网络**: 建分支/push/开 PR/轮询/merge 全部走 GitHub API,断网即中断
- 实施前置: 网络稳定(用户已明确"等网络稳定再实施自动化")
- 网络中断的**降级**: 已跑完的 case 结果已存本地,可断点续跑(`--resume` 跳过已完成 case)
- 当前阶段: 方法论 + 场景素材(fixed/ 快照)先行 commit, 驱动器代码待网络稳定后实现

## 十、实施计划

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 场景定义(changes/ + expected) + 防剧透 + 契约文档 | ✅ 完成 |
| Phase 1.5 | 正样本补 fixed/ 快照 + manifest 注册表 | 本提交 |
| Phase 2 | 驱动器 run_golden_tests.py(Level 0 + Level 1) | 待网络稳定实施 |
| Phase 3 | 复杂缺陷场景(并发/跨函数契约) | 后续 |

## 附: 场景防剧透原则(重申)

1. 代码不剧透(不写自我暴露缺陷的注释); 2. 答案分离(集中在 golden-contract.md);
3. 答案不可见(context_files 只收 AGENTS.md); 4. 自然提交(缺陷藏起来让 AI 自己找)。

---

## 十一、首轮评测记录(2026-08-13)

### Level 0 结果: 6/6 通过(5/6 首轮 → prompt 修复后 6/6)

| case | 结果 | AI 实际表现 |
|------|------|------------|
| case-bug | ✅ | 2 issues, check=failure — nil 解引用 error 命中 |
| case-security | ✅ | 2 issues, check=failure — SQL 注入 + 硬编码密码 |
| case-convention | ✅ | 1 issue, check=success — 吞 error 被报出 |
| case-clean | ✅ | 0 issues(首轮误报 2 warn, 见下) |
| case-docs | ✅ | 0 issues, 质量门 pass |
| case-bait | ✅ | 0 issues(不报 error, 更克制) |

### 过程踩坑(驱动器 bug, 已修)

1. **查错评论通道**: pr-review 整体评论走 `POST /pulls/{n}/reviews`, 驱动器原查 issue comments
   → 永远解析 0 issues(正样本全判 fail, 尽管 check-run 是 failure)。修: 新增 `list_pull_reviews` + 回退。
2. **results/ 未 gitignore**: 场景分支 `git add -A` 会把上个 case 的结果文件误提交进分支, 污染 diff,
   且 checkout 切回 main 时被 git 清理导致保存失败。修: 测试仓库 .gitignore 加 `results/` + 驱动器保存前 mkdir。
3. **远程残留分支冲突**: 手工测试遗留的 test/* 分支导致 push 被拒(non-fast-forward), 驱动器 finally 清理兜底。

### prompt 改进(针对 case-clean 误报)

首轮 case-clean 误报 2 条凑数 warn(range 对 nil 安全却建议处理、自相矛盾的注释建议),
在 SYSTEM_PROMPT 增加规则 13-15 后清零:
- 13: severity 分级克制(宁低勿高, error 需直接导致 bug/漏洞且有证据)
- 14: 语言语义已保证安全的模式不报(range 对 nil 安全等), 报前自问能否被一句话反驳
- 15: 一语中的(title ≤30 字, detail 只说关键, 杜绝发散)

### 当前基线

- 正样本缺陷全命中(bug/security/convention), 负/边界零误报
- 全量 6 场景单轮 ≈ 4-5 分钟(含质量门 judge)
- 驱动器: `ai-tools/golden-tests/run_golden_tests.py --level 0|1`; 结果存 `results/<case>.json` + `report-level0.md`

### Level 1 结果: 6/6 通过, 修复闭环 3/3 完整走通

| case | review#1(拒绝) | 修复后 review#2(放行) |
|------|----------------|----------------------|
| case-bug | failure, 3 issues | agree(success) |
| case-security | failure, 2 issues | agree(neutral) |
| case-convention | failure, 2 issues | agree(success) |
| clean / docs / bait | 0 issues, success | — |

**refuse → fix → agree 闭环自洽验证通过**: 有缺陷时拦下, 按 fixed 快照修复后放行。

### case-convention 漏报排查记录(重要教训)

首轮 Level 1 中 case-convention 连续漏报(吞 error 判为"结构清晰"), 逐层排查:
1. 评论通道 / 上下文注入 / 规则 14 例外 / 审查温度 0.3 —— 均非根因
2. **根因: 人造场景失真** — `loadConfig()` 恒 `return nil`, 吞掉"永远不会失败的 error"
   无实际风险, AI 判"无害"是**合理判断**而非 bug
3. 修复: 场景改为 `os.ReadFile` 真实读取(可能失败), 吞 error = 配置加载失败被静默忽略,
   约定违反真实可检出 → 重跑通过

**结论**: 人造场景的"玩具代码"会导致失真(缺陷设计得不够真实, AI 的合理判断与契约期望冲突)。
这正是后续引入"真实 PR 回放集"评测的核心动机(见真实评测环境方案)。

---

## 十二、真实 PR 回放集(Phase 4 规划, 2026-08-13 草案)

> 状态: **方案草案, 待用户确认后实施**。目的: 用真实 PR 的 diff + 真实修复提交作为评测素材,
> 摆脱人造场景"玩具代码失真"与样本量不足的局限, 让评测贴近实战。

### 12.1 动机

- 人造场景仅 6 个、Go-only、缺陷类型简单; case-convention 漏报证明人造"玩具代码"会失真
  (return nil 的 error 吞掉无实际风险, AI 判无害合理)
- 真实 PR 蕴含: 真实 diff 复杂度、多语言(devops-dashboard 为 Go + React/TS)、真实修复轨迹
- PR 越多, 回放集越大, 评测越可信 → 形成"PR 积累 → 复盘 → 改进 → 回归"的敏捷循环

### 12.2 场景结构(复用 case 三要素)

```
playback/<repo>-<pr号>/
├── changes/        # 合并前最后一次 push 的 diff(apply 到基线快照)
├── fixed/          # 合并后的代码
├── expected.json   # 期望(以"修复提交实际改了什么"为准, 见 12.3)
└── meta.json       # 语言/PR 标题/描述/修复提交列表(含人工 review 结论, 防剧透不进上下文)
```

驱动器 `run_golden_tests.py` 复用(目录结构一致), 差异仅在场景来源。

### 12.3 ground truth 的定义(关键)

**以"修复提交"为主, 人工 review 评论为辅**:

- 修复提交 = 合并前最后一次 push 到合并 commit 之间的 diff——开发者承认并修了的问题, 客观证据
- 人工评论用于补充标注(哪些修复对应哪些审查意见)
- "AI 漏报"的硬定义: 开发者后来修了, 而 AI 当时没报

### 12.4 采集与复盘流程(事后, 非实时)

```
① 采集: 每周扫描目标仓库已合并 PR(GitHub API)
② 提取: 合并前 diff(changes) + 合并后代码(fixed) + 修复提交清单
③ 人工校准: 维护者过一遍 ground truth 清单(每周期 ~10 分钟, 半自动)
④ 生成场景: playback/<repo>-<pr号>/ → 复用驱动器评测
⑤ 出改进需求: AI 漏报/误报归类 → backlog → 按优先级实现 → 回归
```

### 12.5 敏捷演进周期(建议两周一轮)

- 第 1 周: PR 积累 + 采集(自动化, 零人工)
- 第 2 周: 人工校准(10 分钟) + 评测 + 产出改良需求 + 实现高优先级项
- 人造场景(6 个)全程做回归, 防退化; 回放集提供"需求来源"

### 12.6 接入方配合(零改造)

- devops-dashboard 等被审查仓库**不需要任何改动**; ai-review workflow 照常跑
- 3 条建议(好习惯): ①修复尽量在同一 PR 内完成(修复提交留在 PR 历史); ②PR 描述写一句"本次修复了 X";
  ③仓库根有精简的 AGENTS.md(约定上下文 + 复盘判定标准)

### 12.7 待确认项

- [ ] 回放集起步源: devops-dashboard(GitHub 镜像)vs ai-tools 自己
- [ ] ground truth 口径: 修复提交为主(推荐)vs 人工评论为主
- [ ] 周期节奏: 两周一轮是否合适
- [ ] 采集器实现位置: ai-tools/golden-tests 新增 playback 采集模块
