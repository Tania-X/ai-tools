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

### 3.1 语言先验误读：用别的语言的直觉读 Python

A–F 六类里，**有相当一部分的根因是「语言语义先验」**：模型把 Java / JS 的默认规则套到
Python 上，而 Python 的规则恰好不同。单独列出来是因为它**可以被确定性手段挡住**
（技术栈声明 + 语义检查清单 + 针对性 golden 用例），比「让模型更聪明」可靠得多。

| 实例（本仓真实误报） | 模型套用的先验 | Python 实际规则 |
|---|---|---|
| R7 s4「领域错误会返回 500」 | Java：异常必须在**每个方法/路由**里被 try/catch，否则一路向上变成 500 | Python 允许**按类集中注册处理器**（`@app.exception_handler(X)`），Starlette 抛异常后沿 MRO 找最精确的处理器 → `DomainError→400` 已覆盖 |
| R7 s5「空 `data_sources` 被丢弃」 | JS：`[]` 与 `{}` 都 falsy，`if (filters)` 会跳过 | Python 真值判断看**容器是否非空**：`{"data_sources": []}` 是**非空 dict** → `if filters:` 为真 → 请求体带上了 filters |
| R1 s4「未传 `base_url` 会打到硬编码 localhost」 | Java：无参构造 = 用默认常量 | Python 的 `x or default` 惯用法里 default 是**从配置读**的（`settings.openrag_base_url`） |
| R1 s4「`await` 在 `with` 块内 = 文件先被删除」 | Java：`try-with-resources` 关闭时机与内部调用交错 | 需要实际执行 `with` 语句的语义：块内 `await` 完成后才 `__exit__`，`delete=True` 是关闭时 unlink |
| R8 s4「`PermissionDeniedError` 被 `DomainError` 处理器覆盖成 400」 | Java：`catch` 顺序敏感、父类在前会遮蔽子类 | Starlette 处理器查找**顺序无关**、按 MRO 取最精确；模型自己的正文也写了「这一点没问题」（→ 同时属 F 类） |
| R6 s4「非法 `tenant_id` 会 500」 | Java/Scala：值对象的构造器会校验并抛异常 | 普通 `@dataclass(frozen=True)` **不校验**；`TenantId("abc")` 合法，查不到只是 404 |
| R2 s3「迁移用 `asynccontextmanager(get_session)` 包裹」 | Java：无对应概念，看起来像多余包装 | `get_session` 是 **async generator**，`async with get_session()` 直接 `TypeError`，包一层是必需的 |
| 测试辅助函数传错类型（我实际写出的 bug：`role_id=role` 应为 `role.id`） | Java：编译期就会报类型不匹配 | Python 动态类型，错类型延迟到运行时才炸 → **缺静态检查时，评审被迫兼职编译器**，而它对动态类型的判断并不可靠 |

> 归纳：**误报高发区 = 「正确的 Python 代码 + 与 Java/JS 直觉相反的语言规则」**。
> 这类误报不会因为模型变大而消失，应该用规则与用例固化掉。

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

### P0-4 技术栈声明 + 语言语义确定性检查（命中 §3.1 全部实例）

- 落点：`pr-review/pr_review/config.py`（新增 `stack` / `stack_notes`，可由仓库声明，也可从 diff 后缀推断）
  + `pr_review/prompt.py`（注入一段「语言语义提醒」）+ `golden-tests`（新增语言陷阱用例集）
- 做法：
  ```text
  1. 在 prompt 里声明技术栈（language / framework / async runtime），并附上高发语义清单：
     Python: 真值判断（非空容器为真）、异常可按类集中注册（框架级 handler 沿 MRO 匹配）、
             async with / async generator 的生命周期、dataclass 不校验、动态类型无编译期检查
  2. 断言「违反语言语义」的 finding 必须附语义依据（引用规则或可复现片段），
     否则按 P0-1 的可验证性规则降级
  3. golden-tests 增加 case-semantics-*：代码正确但极易被跨语言直觉误报的样本
     （空容器真值、框架级异常映射、async generator、dataclass default_factory、`x or default` 读配置）
  ```
- 代价：配置项 + 一段 prompt 文本 + 4~6 个 golden 用例
- 为什么有效：§3.1 那 8 个实例全部落在同一模式里，属于**可用规则消灭**的误报类别；
  同时它能减少「同一断言跨轮升级」（C 类）——因为语义争议一次就被解决

### P0-5 职责边界：先问「这件事该由谁负责」，再决定报不报（用户提出，覆盖全部类别）

用户在复盘时给出一条判据，我认为应当提升为 pr-review 的**总纲**：

```text
编译器/类型检查器能确定的   → 交给 mypy / ruff（确定、便宜、可复现）
运行时行为                 → 交给测试（pytest）
语义 / 设计取舍             → 交给人
AI 评审                     → 只应负责上面三类【之外】的部分
```

这条为什么能治本：我们的 15 轮里，**误报几乎全部落在前三类的职责边界内**——
"这个类型/框架语义对不对"（类型检查器与文档的职责）、"这段代码运行时会不会
走那条分支"（测试的职责）、"这个设计是否有意为之"（人的职责）。
AI 评审被迫回答本该由前三者回答的问题时，只能靠语言先验去猜，于是产生 §3.1 的 8 个实例。

**机械化的做法（不要只写进 prompt 当口号）**：

```text
1. 让仓库声明自己的工具链（.ai-review.yaml 增 toolchain 段：ruff / mypy / pytest 的命令）
2. 审查前把工具链的真实结果作为上下文注入：
     - CI 已通过的检查（ruff / mypy / pytest 全绿）→ 明确告诉模型"这些维度已被机器确认"
     - 若模型仍要报"类型不对 / 测试没覆盖的行为 / 某个断言会失败"，
       必须回答一句：为什么现有工具没拦住它（否则确定性降级到 2）
3. 分类映射到门禁：
     category=convention（风格/约定）→ 建议级，不计门禁
     category=design_intent（语义/取舍）→ 需人工确认，不计门禁（仓库已有此规则）
     其余（bug/security/resource）→ 才可进入门禁
4. golden-tests 增 case-toolchain-covered：
     diff 里放一个 mypy 能抓的类型错误，且声明仓库有 mypy → 审查应【不报】
     （理由：CI 会失败，评审报它是重复劳动且可能报错；真报也要附"为什么 mypy 漏了"）
```

**代价**：配置项 + 注入工具链结果一节 prompt + 1~2 个 golden 用例。
**收益**：把"猜测型误报"从源头移出评审的职责范围——这比调 severity 阈值更根本。

> 同源观察（本仓实证）：我实测这份 Python 仓库（openrag-lab）在有 ruff+pytest、
> 无类型检查的情况下，加上 mypy 立刻抓到 7 处类型不一致（其中 2 处是"靠运行时宽容
> 掩盖标注不一致"的写法）。这类问题在 15 轮评审里**一次都没有被正确报出**过——
> 不是评审太弱，而是这类问题本来就不该问它。

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
| 语言先验误报 | §3.1 中「正确 Python 代码被按他语言规则判错」的条数 | 8 / 15 轮中出现（跨 4 个类别） | ≤ 1，靠 P0-4 的规则与用例收敛 |

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
第一批（改动小、直接命中 53% 里的 6 条）：P0-1 可验证性字段、P0-3 判官故障留档、
        P0-4 技术栈声明 + 语言语义检查（§3.1 的 8 个实例全部命中）
        P0-5 职责边界（总纲：先判断该由谁负责，再决定报不报；与 P0-4 互补）
第二批（需要状态设计）：P0-2 声明台账与跨轮指纹
第三批（需要仓库侧配合）：P1-1 上下文分级、P1-2 不变量清单、P1-3 判定回流
第四批（成本/体验）：P2-1 健康度 footer、P2-2 轮次预算与增量审
```
