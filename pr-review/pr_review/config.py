"""审查行为配置(.ai-review.yaml)。

与 gateway 配置(模型/key)分离:这里只控制"审什么、多细、发不发"。
参考 .coderabbit.yaml 的哲学:初版先 chill,控制噪音。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # PyYAML 是 pr-review 唯一新增运行时依赖
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时给出可读错误
    yaml = None  # type: ignore[assignment]

# 严重级别从高到低
SEVERITIES = ("error", "warn", "info")


@dataclass
class ReviewConfig:
    # 审查重点(直接作为指令进入 prompt)
    review_focus: list[str] = field(
        default_factory=lambda: [
            "bug 与逻辑错误",
            "并发/性能隐患",
            "安全问题(注入/越权/密钥泄露)",
            "资源泄漏(连接/文件未关闭)",
            "明显不符合项目既有约定(命名/分层/异常处理)",
        ]
    )
    # 忽略的路径(glob, 支持 **)
    ignore_paths: list[str] = field(
        default_factory=lambda: [
            "**/*.lock",
            "**/package-lock.json",
            "**/pnpm-lock.yaml",
            "**/yarn.lock",
            "**/*.min.js",
            "**/*.min.css",
            "**/vendor/**",
            "**/generated/**",
            "**/dist/**",
            "**/build/**",
            "**/target/**",
            "**/.idea/**",
            "**/.vscode/**",
        ]
    )
    # 只发出达到该级别及以上的问题(error > warn > info)
    min_severity: str = "warn"
    # 合并门禁:存在达到该级别及以上的问题时, check-run 失败 + job exit 1(PR 变红)
    #   error: 只有 error 拦(推荐) | warn: warn 也拦 | off: 永不拦(只发评论)
    fail_on_severity: str = "error"
    # 切片:每批最多文件数(大 PR 分批审,控制单次 prompt token)
    max_files_per_batch: int = 20
    # 单文件 patch 超过该行数则截断(在评论中提示)
    max_lines_per_file: int = 800
    # 是否在评论中附带模型/token/成本统计
    show_stats: bool = True

    def severity_rank(self, severity: str) -> int:
        s = (severity or "info").strip().lower()
        if s not in SEVERITIES:
            return 0  # 未知级别按最低处理
        return SEVERITIES.index(s)

    def passes_filter(self, severity: str) -> bool:
        """是否达到 min_severity 门槛。"""
        return self.severity_rank(severity) <= self.severity_rank(self.min_severity)

    def should_ignore(self, path: str) -> bool:
        from fnmatch import fnmatch

        p = path.replace("\\", "/")
        for pat in self.ignore_paths:
            if fnmatch(p, pat):
                return True
            # fnmatch 中 "**/" 需要路径含斜杠,补一次根路径匹配:
            # "**/package-lock.json" 也要命中仓库根的 package-lock.json
            if pat.startswith("**/") and fnmatch(p, pat[3:]):
                return True
        return False


DEFAULT_CONFIG = ReviewConfig()


def load_config(path: str | Path | None = None) -> ReviewConfig:
    """加载 .ai-review.yaml;文件缺失/为空时使用默认配置。

    注意:返回全新实例,绝不修改模块级 DEFAULT_CONFIG 单例。
    """
    cfg = ReviewConfig()  # 默认值与 DEFAULT_CONFIG 一致,但独立可变
    if yaml is None:
        return cfg
    if path is None or not Path(path).is_file():
        return cfg

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    focus = data.get("review_focus")
    if focus:
        cfg.review_focus = list(focus)
    ignore = data.get("ignore_paths")
    if ignore:
        cfg.ignore_paths = list(ignore)
    if data.get("min_severity") in SEVERITIES:
        cfg.min_severity = data["min_severity"]
    if data.get("fail_on_severity") in SEVERITIES + ("off",):
        cfg.fail_on_severity = data["fail_on_severity"]
    cfg.max_files_per_batch = int(data.get("max_files_per_batch", cfg.max_files_per_batch))
    cfg.max_lines_per_file = int(data.get("max_lines_per_file", cfg.max_lines_per_file))
    cfg.show_stats = bool(data.get("show_stats", cfg.show_stats))
    return cfg
