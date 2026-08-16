# L3 双引擎对比评测:自研引擎 vs Claude Code + repo-tools MCP

> 2026-08-16 · feat/agentic-production 分支
> 目的: 量化验证「把自研审查工具封装为 MCP server」的价值——同一 9 个 golden 场景,
> 第三方主流 agent(Claude Code, DeepSeek 驱动)通过我们的 MCP 工具审查, 与自研引擎对比。

## 评测方法

- **评测集**: golden-tests 的 9 个人造场景(正样本 6 + 负样本 2 + 边界 1)
- **引擎 A(自研)**: pr-review 引擎(带完整 SYSTEM_PROMPT 策略 + 内置 repo_tools)
- **引擎 B(外部)**: Claude Code(`deepseek-v4-flash` 驱动, **无任何审查策略注入**, 只有 repo-tools MCP 的 4 个工具)
- **流程**: 基线代码复制到仓库(可用工具查) → 生成 diff → 引擎 B 审查 → 结果按 golden expected 判定

## 9 场景对比结果

| 场景 | 期望 | 自研引擎 | Claude Code + MCP | 判定 |
|------|------|---------|-------------------|------|
| case-bug | 报 error(nil 必然 panic) | ✅ 1 error | ✅ 2 error/warn + 1 info | **对齐** |
| case-security | 报出(注入+硬编码密码) | ✅ 2 issues | ✅ 2 error + 2 warn | **对齐** |
| case-convention | 报出(约定违反) | ✅ 1 issue | ✅ 2 warn + 2 info | **对齐** |
| case-clean | 0 误报 | ✅ 0 | ✅ 未发现问题 | **对齐** |
| case-docs | 0 误报 | ✅ 0 | ✅ 未发现问题 | **对齐** |
| case-bait | 不报 error | ✅ 不报 | ✅ 0 error(1 warn + 3 info) | **对齐** |
| case-merge-locations | 合并为 1 条 | ✅ 1 条 | ✅ 1 条(3 处位置合并) | **对齐** |
| case-severity-security | warn 不 error | ✅ warn | ✅ **warn**(自发) | **对齐** |
| case-refactor-context | 主动查代码报跨文件 | ✅ 1 warn | ✅ error(忽略 id + 破坏调用方) | **对齐** |

**9/9 对齐。** 引擎 B 未注入任何审查策略, 表现与带完整策略的自研引擎一致。

## 关键发现

### 1. 严重度判断「自发」对齐我们的策略
引擎 B 无 SYSTEM_PROMPT, 但:
- case-severity-security(错误忽略+安全后果) → 定 **warn**, 并注明"边界上可视为 error"
- case-merge-locations(必然触发的契约不一致) → 定 **error**, 明确"必然触发, 不是假设路径"

与我们 2026-08-15 定的策略(必然触发→error, 假设性故障路径→warn)**完全一致**——
说明该策略符合模型的自然判断, 不是拍脑袋的规则。

### 2. 主动调用工具完成跨文件排查
引擎 B 在 9 个场景中**全部主动调用 repo-tools 工具**(grep / read_file / list_dir, 部分用 ast_grep):
- case-refactor-context: 查到未变更的 display.go 契约, 发现 getUserName 破坏调用方
- case-merge-locations: 确认 3 处调用点同一根因, 主动合并
- 每个场景都做了"影响面确认"(grep 调用方), 这是只看 diff 做不到的

### 3. 合并与收敛行为自发成立
case-merge-locations 引擎 B 自行合并 3 处为 1 条(未注入同根因合并规则)。

### 4. 额外发现力
case-refactor-context 引擎 B 额外发现"id 死参数"(忽略参数恒定返回值),
并给出两种修复方案——细节发现甚至超过自研引擎。

## 结论(面试叙事)

> 我自建了 agentic 代码审查引擎, 并把审查工具集封装为标准 MCP server。
> 用同一套 9 场景评测集做**双引擎对比**: Claude Code 通过我的 MCP 工具,
> 在零策略注入下达到了与自研引擎(带完整策略)一致的 9/9 审查质量——
> 且严重度判断自发符合我沉淀的审查策略。

这证明了: ①MCP 封装真实可用(工具被主流 agent 用好了); ②自研评测资产可复用
(同一套场景评测第三方 agent); ③审查策略符合行业模型的自然共识。

## 复现

```bash
# 前置: claude 已配置 repo-tools MCP(见 mcp_server.py 顶部说明)
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_API_KEY=<deepseek key>
export ANTHROPIC_MODEL=deepseek-v4-flash
cd ai-tools && python l3-eval/run_claude_eval.py        # 全 9 场景
python l3-eval/run_claude_eval.py case-bug             # 单场景
# 结果在 l3-eval/results/<case>.txt
```
