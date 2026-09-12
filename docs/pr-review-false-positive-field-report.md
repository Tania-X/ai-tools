# pr-review 误报现场报告（真实 PR 实测，2026-09-12）

> 定位：golden-tests 是**合成**用例（受控 diff、已知答案）；本文件是**真实战场**数据——
> 两个连续功能 PR、共 15 轮评审、15 条门禁级（severity ≥ 4）finding 的逐条定性。
> 目的：记录现有机制（context_files / 两轴严重度 / 质量门逐条验证 / 线程决议 / judge 分离）
> **没能拦住**的误报模式，并给出可落地的改进建议（含具体文件与代价）。
>
> 结论先行：15 条门禁级 finding 里 **8 条属误报 / 设计意图 / 跨轮重复升级（≈53%）**，
> 7 条为真（全部在合并前修复，零逃逸）。代价主要是**人工/agent 的分诊时间**，不是 token 成本。

## 1. 数据来源与口径

| 项 | 值 |
|---|---|
| 仓库 | `openrag-lab`（多租户 RBAC + 检索隔离，Python/FastAPI） |
| PR | #8 `feat/rbac-tenant-s1p3b`（8 轮）、#9 `feat/rbac-tenant-s1p3c`（7 轮） |
| 模型 | `deepseek-chat`（审查 + judge 同模型不同角色） |
| 配置 | 仓库 `.ai-review.yaml` 默认：`min_severity=2`、`require_fix_severity=3`、`fail_on_severity=4` |
| 定性口径 | 按**断言本身是否成立**分类，不看它是否顺带带来改进：<br>`real` 成立且有真实触发路径；`false` 断言与代码/实机不符；`by-design` 已在文档与测试中被声明为有意行为；`duplicate` 与前面轮次同一断言 |

消费方式：审查结果由 agent 逐条核对（读代码 + 跑测试 + 实机复现）后再决定修或不修，
每次反驳都有测试或实机输出作为证据（见 §5 反驳模板）。

## 2. 逐轮数据

### PR #8（s1p3b，8 轮）

| 轮 | 门禁 check | 门禁级 finding | 定性 |
|---|---|---|---|
| R1 | failure | gateway 未传 `base_url`，会打到默认 localhost | **false**（`OpenRAGClient` 兜底读的是配置；顺带暴露真实覆盖盲区） |
| R2 | failure | `RuntimeError` 未映射 → 500 | real |
| R3 | failure | 遗留 `/api/documents` 无鉴权泄露全库文件名 | real |
| R4 | neutral | —（2 条 s2） | — |
| R5 | success | — | — |
| R6 | failure | ① 被禁用租户的 super_admin 仍可跨租户 ② 非法 `tenant_id` 会 500 ③ `data_sources` 无上限 | ① real ② **false**（`TenantId` 无校验，实际 404）③ real（可用性） |
| R7 | failure | ① 空 `data_sources` 被 client 丢弃 → 隔离失效（**s5 致命**）② super_admin 无 `search:use` 越权 | ① **false**（把 `{"data_sources": []}` 当成空字典；dict 有一个键即真）② **false**（super_admin 全局角色本就持有全部权限） |
| R8 | failure | `PermissionDeniedError` 被 `DomainError` 处理器覆盖成 400 | **false**（标题被其自身正文推翻：「按 MRO 精确匹配…这一点没问题」） |

门禁级 9 条：real 4 / false 5（**56%**）。

### PR #9（s1p3c，7 轮）

| 轮 | 门禁 check | 门禁级 finding | 定性 |
|---|---|---|---|
| R1 | failure | 临时文件在入库完成前被删除 | **false**（上传与等待都在 `with` 块内；顺带暴露 Windows 可移植性） |
| R2 | failure | super_admin 可借 `tenant_id` 删除他人租户文档 | **by-design**（已在契约与文档声明，review 自己写「不构成越权」） |
| R3 | failure | 重传不更新 `updated_at`，契约字段恒为创建时间 | real |
| R4 | neutral | —（judge 输出无法解析，评分 0/100） | 判官故障 |
| R5 | failure | 上传不校验文件类型（`.exe` 也进解析器） | real |
| R6 | failure | 迁移写登记表的 `openrag_document_id` 恒为 None | real |
| R7 | failure | 领域校验失败返回 500 而非契约的 400 | **false + duplicate**（与 R6 的 s2 同一句话，本轮升级为 s4） |

门禁级 6 条：real 3 / false-by-design-duplicate 3（**50%**）。

### 汇总

| 指标 | 实测 |
|---|---|
| 门禁级 finding 总数 | 15 |
| 误报 / 设计意图 / 重复升级 | **8（53%）** |
| 真实缺陷 | 7（全部合并前修复，零逃逸） |
| judge 输出无法解析 | 2 / 15 轮（**13%**，两次都发生在第 4 轮） |
| 同一断言跨轮升级 | 1 次（R6 s2 → R7 s4，直接导致门禁红） |
| 标题与正文自相矛盾 | 1 次 |
| 轮次成本 | 单轮 27k–80k token、¥0.007–0.028（真正的成本是分诊） |

## 3. 误报分类学（按可修复性排序）

| 类别 | 典型表现（本仓实例） | 为什么会发生 | 对工具的启示 |
|---|---|---|---|
| **A 事实性错误** | `{"data_sources": []}` 被当成空字典；`base_url` 兜底被当成硬编码；`await` 在 `with` 块内被说成块外删除 | 模型只读代码文本，不做语义执行；对 Python 真值/作用域规则、库内部兜底的推理出错 | 要求**给出可反驳的证据**（行号 + 代码片段 + 复现路径），而不是叙述 |
| **B 假设推演当严重问题** | 「长度 512 可能溢出」「Windows 上可能 PermissionError」「进程被 kill 会残留」被标 severity 4 | 两轴设计里 trigger 应由模型自评，但模型倾向把推演标成 `real` | 只有 `trigger=real` 才允许 ≥3；`evidence` 里没有真实触发路径 → 强制降级（确定性信号，不靠模型自评） |
| **C 同一断言跨轮升级** | R6 s2「领域错误会 500」→ R7 s4 同句升级，门禁变红 | 每轮对**全量 diff** 独立重审；线程决议去重依赖人在线程里回复，而实际流程是「改代码 → push → 重新审」 | 建**声明台账**：按 (`file`, 归一化主张) 指纹跨轮记录，重复出现需附**新证据**才允许升级 |
| **D 设计意图当漏洞** | super_admin 跨租户（删除/搜索/建用户）被反复标 security | 授权模型写在文档与测试里，但 `context_files` 预算（8000 字符）截断，且「已声明 + 已测」这一层信息不在文档里 | 仓库提供**机器可读的不变量清单**（主张 + 对应测试名），并要求：断言违反前先确认是否存在对应测试 |
| **E 判官故障** | judge 返回不可解析内容 → 0/100，整轮结论不可用 | judge 输出无 schema 强约束、无原始输出留档 | 解析失败：留档原文 + 重试一次（纯 JSON） + 计入健康度指标；对下游的影响面要明确（应只影响「质量评分」，不影响 issue 门禁） |
| **F 标题与结论矛盾** | 标题说「被 400 处理器覆盖」，正文说「这一点没问题」 | 模型先写标题再自我修正，正文推翻标题 | 报告层做**标题/正文一致性**确定性校验：正文含「没问题/不成立/不构成」而标题为 defect → 降级为 comment |

## 4. 现有机制为何没拦住（逐条对照）

| 已有机制 | 状态 | 缺口 |
|---|---|---|
| `context_files` 注入（AGENTS/README/docs/spec） | ✅ 已实现 | 预算 8000 字符 + `docs/**/*.md` 全量 → 大仓里关键契约被**截断**；「已声明且已测」信息不在文档里 |
| 两轴严重度（`hypothetical → 2`） | ✅ 已实现 | 触发识别依赖模型自评 `trigger`，实测它会把推演标成 `real`（R1/R5/R7） |
| 质量门逐条验证（删除/降级/修正 + 降级哨兵） | ✅ 已实现 | 输入是结构性信号（行号越界、假设措辞），对**语义读错**（A 类）无能为力 |
| 线程决议去重（resolve/ignore） | ✅ 已实现 | 依赖人回复线程；「改代码即 push」的循环里不生效（C 类） |
| judge 与审查者分离 | ✅ 已实现 | judge 自身崩溃无兜底留档；两次 0 分都发生在第 4 轮（可能与上下文长度/重写次数有关，值得单独看） |
| `fail_on_severity=4` 门禁 | ✅ 已实现 | 误报一旦被标 ≥4 就直接把 check 变红（R7），而「需人工确认」类**不计入门禁**的规则救不了它——因为它的分类是 `bug` 不是 `design_intent` |

## 5. 改进建议（按 ROI，含落点）

### P0-1 可验证性字段（命中 A / B / F）

- 落点：`pr-review/pr_review/prompt.py`（issue schema 增加 `evidence`、`verification`）+ `pr_review/quality.py`（`structural_signals` / `per_issue_verify` 增加确定性规则）
- 规则：
  ```text
  severity >= 3 时必须有 evidence（file:line + 代码片段，而非叙述）
  severity >= 4 还必须给出 verification 三选一：
    ① 能复现的具体输入/命令  ② 与之矛盾的现有测试名（说明为什么该测试挡不住）
    ③ "none: 属推演"
  verification = ③ 且 severity >= 4 → 确定性降级到 2（不整批重写，逐条处理）
  ```
- 代价：schema + 两条确定性规则；会略微增加输出长度
- 为什么有效：本仓 8 条误报里 A/B/F 共 6 条，都是「无法给出可反驳证据」的断言

### P0-2 声明台账与跨轮指纹（命中 C / D）

- 落点：`pr-review/pr_review/review.py`（新增 ledger 模块）+ PR 内隐藏标记注释（或 workflow cache）
- 规则：
  ```text
  指纹 = sha1(file + 归一化主张 + 涉及的符号名)
  台账记录：指纹 → 首现轮次、当时 severity、人类/agent 处置（fixed / by-design / false）
  下一轮遇到同指纹：
    · 已标 by-design / false → 不报（或仅以「此前已评估」折叠列出）
    · 想提高 severity → 必须带新证据字段，否则维持原级
  ```
- 代价：中等（多一次状态读写）；需要定义「归一化」口径
- 为什么有效：C 类（R6→R7 升级）是唯一让门禁由绿转红的误报路径

### P0-3 判官故障处置（命中 E）

- 落点：`pr_review/quality.py::_parse`
- 做法：解析失败 → 原文写进评论折叠块/artifact；重试一次（要求纯 JSON，去掉解释性前缀）；
  失败计数进入统计 footer；明确**不改变 issue 门禁**（评分与门禁解耦）
- 为什么有效：2/15 轮整体判断不可用，是「整轮浪费」而不是单条噪音

### P1-1 上下文分级注入（命中 D）

- 落点：`pr_review/config.py::context_files`（支持权重/单文件上限）+ `context.py` 采集顺序
- 做法：允许 `context_files` 写成 `{path, max_chars, priority}`；优先注入「契约/不变量清单」这类短而密的文件；
  预算超限时按优先级截断而不是按 glob 顺序
- 为什么有效：本仓 `api-contract.md` 300+ 行、`docs/rbac-tenant-ddd-design.md` 900 行，8000 字符只能装很小一部分

### P1-2 不变量清单 + 对应测试名（命中 D，长期解）

- 落点：`config.py` 新增 `invariants`（或约定文件 `AI-REVIEW-INVARIANTS.md`），注入 prompt；`golden-tests` 增加 `case-invariant`
- 形式：
  ```yaml
  invariants:
    - claim: "super_admin 是全局根角色，可跨租户读/建用户/删文档；非超管一律 403"
      tests: ["tests/rag/test_document_endpoints.py::test_super_admin_may_delete_in_another_tenant"]
    - claim: "领域错误由 interfaces/api/errors.py 统一映射（DomainError→400），路由不重复捕获"
      tests: ["tests/rag/test_document_endpoints.py::TestStatusCodeContract"]
  ```
- 为什么有效：把「设计意图 + 已测」变成模型可读的事实，而不是让它从长文档里猜

### P1-3 人类判定回流（命中 C / D 的长期解）

- 落点：`pr_review/reply.py`（线程回复支持结构化标签）+ 台账
- 做法：`/ai-review by-design "<一句理由>"`、`/ai-review false-positive`、`/ai-review fixed`；
  存进该仓库的少量 few-shot（每条 ≤200 字符，最多 N 条）注入后续 prompt
- 为什么有效：误报是**仓库相关**的（这只仓的授权模型、错误映射、命名空间规则反复被误读），一次性喂进去比每次重新解释便宜

### P2-1 每轮健康度 footer

- 评论末尾输出：issues 数 / 门禁级数 / judge 分与是否解析失败 / 本轮命中台账指纹数
- 用途：让消费者一眼判断「这轮值不值得细看」，也给我们自己留回归指标

### P2-2 单 PR 轮次预算与增量审

- 超过 N 轮后，只审**新增 commit 的 diff**，不再全量重审
- 为什么有效：C 类与部分 D 类都源自全量重审；同时省 token

## 6. 度量与回归

| 指标 | 定义 | 现状 | 目标 |
|---|---|---|---|
| 门禁级误报率 | (false + by-design + duplicate) / 门禁级 finding 总数 | ~53%（15 条中 8 条） | ≤ 20% |
| 跨轮同断言升级 | 同一指纹在不同轮次提高 severity 的次数 | 1 / 15 轮 | 0 |
| judge 解析失败率 | 解析失败轮次 / 总轮次 | 2 / 15（13%） | 0，且失败时不影响 issue 门禁 |
| 逃逸缺陷 | 合并后才发现的真实缺陷 | 0 | 保持 0 |

回归方式：
- 合成侧：`golden-tests` 现有 precision/recall 保留；新增 `case-invariant`（声明不变量、diff 未违反 → 必须 0 误报）与 `case-repeat-claim`（同一 PR 两轮，第二轮必须不重复升级）
- 真实侧：每完成一个真实 PR，把轮次数据追加到本文档表格（口径固定为 §1），用于比较改进前后

## 7. 不建议做的

- **不要追求零误报**：目标是把「门禁级误报」压到接近 0；轻微噪音（s2）成本低于继续调优
- **不要让模型自己改 severity 后再判定**：self-report 正是 B 类的成因；降级应由**确定性信号**触发
- **不要靠换更强模型硬解**：本仓的误报多是「可验证性缺失」，先加 schema 与台账，性价比更高
- **不要把所有误报都塞进 prompt 上下文**：会挤掉真正的契约内容；按仓库沉淀少量高价值 few-shot 即可

## 8. 消费者侧（agent/人）可复用的处置流程

本仓 15 轮里这套流程把误报处理成本压到了可接受范围：

```text
1. 把每条 finding 当假设，不当结论
2. 先用最小成本证伪或证实：读相关代码 → 跑相关测试 → 必要时实机复现（本仓常用 openrag-lab 的 TestClient + 真实 OpenRAG）
3. 证伪的：补一条「能挡住这个误解」的测试（把否定结论变成回归资产）
4. 属设计意图的：补代码注释 + 契约文档一句，并加一条断言该意图的测试
5. 只对「成立且有真实路径」的项改代码
6. 每轮把定性结果记下来（本文件即是），供后续评估工具改进
```

## 9. 待用户拍板的落地顺序

```text
第一批（改动小、直接命中 53% 里的 6 条）：P0-1 可验证性字段、P0-3 判官故障留档
第二批（需要状态设计）：P0-2 声明台账与跨轮指纹
第三批（需要仓库侧配合）：P1-1 上下文分级、P1-2 不变量清单、P1-3 判定回流
第四批（成本/体验）：P2-1 健康度 footer、P2-2 轮次预算与增量审
```
