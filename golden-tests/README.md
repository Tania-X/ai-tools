# golden-tests 驱动器

自动化运行 [ai-review-golden-tests](https://github.com/Tania-X/ai-review-golden-tests) 的测试场景,
逐 case 执行 "PR → review → 断言 → 存结果 → 清理" 循环, 量化 pr-review 的审查质量。

## 前置条件

1. **golden 测试仓库的本地 clone**(内含 scenarios/ 场景定义)
2. **GitHub PAT**(需要有该仓库的 pull request + checks 权限)
3. **网络稳定**(建分支/push/开 PR/轮询全部走 GitHub)

## 用法

```bash
cd ai-tools

# Level 0: 快照评测(逐 case: PR → review → 断言 → 关 PR → 下一个)
GITHUB_TOKEN=<你的PAT> python golden-tests/run_golden_tests.py \
    --repo-dir ../ai-review-golden-tests

# Level 1: 含修复闭环(buggy→refuse→fixed→agree→merge, 仅正样本)
GITHUB_TOKEN=<你的PAT> python golden-tests/run_golden_tests.py \
    --repo-dir ../ai-review-golden-tests --level 1

# 只跑指定 case / 断点续跑 / dry-run 预览
python golden-tests/run_golden_tests.py --repo-dir ../ai-review-golden-tests \
    --cases case-bug,case-security
python golden-tests/run_golden_tests.py --repo-dir ../ai-review-golden-tests --resume
python golden-tests/run_golden_tests.py --repo-dir ../ai-review-golden-tests --dry-run
```

## 选项

| 选项 | 说明 |
|------|------|
| `--repo-dir` | golden 仓库本地 clone 路径(必填) |
| `--level 0\|1` | 测试层级(默认 0) |
| `--cases` | 逗号分隔的 case 白名单(默认全部) |
| `--repo` | GitHub 仓库 owner/name(默认 Tania-X/ai-review-golden-tests) |
| `--token` | PAT(默认读 GITHUB_TOKEN 环境变量) |
| `--resume` | 断点续跑(跳过已有 results/<case>.json 的 case) |
| `--dry-run` | 只打印计划, 不执行 |

## 输出

- `results/<case>.json` — 每个 case 的原始审查结果(issues 统计 + check 结论 + 断言判定)
- `results/report-level<N>.md` — 汇总报告(场景矩阵 + 通过/失败 + 失败详情)
- 退出码: 有 fail → 1, 全 pass → 0(可接 CI)

## 结构

```
golden-tests/
├── run_golden_tests.py    # CLI 入口
├── golden/
│   ├── parser.py          # 评论 Markdown → issues 统计(纯函数)
│   ├── assert_result.py   # 期望 vs 实际(纯函数)
│   ├── gitops.py          # git 操作封装
│   ├── github_api.py      # GitHub API 封装
│   ├── runner.py          # Level 0/1 编排
│   └── report.py          # 报告生成
└── tests/                 # 纯函数单测(零网络)
```

完整方法论见 `docs/golden-testing.md`。
