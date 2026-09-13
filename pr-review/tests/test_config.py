"""审查配置加载 / 过滤逻辑单测。"""

import logging

import pytest

from pr_review import config as config_mod
from pr_review.config import DEFAULT_CONFIG, load_config


def test_default_config_sane():
    cfg = DEFAULT_CONFIG
    assert 1 <= cfg.min_severity <= 5
    assert cfg.max_files_per_batch > 0
    assert len(cfg.review_focus) > 0
    assert "**/vendor/**" in cfg.ignore_paths


def test_should_ignore_glob():
    cfg = DEFAULT_CONFIG
    assert cfg.should_ignore("package-lock.json")
    assert cfg.should_ignore("frontend/package-lock.json")
    assert cfg.should_ignore("vendor/foo/lib.go")
    assert not cfg.should_ignore("src/main.go")
    assert not cfg.should_ignore("docs/design.md")


def test_severity_filter():
    cfg = DEFAULT_CONFIG
    assert cfg.passes_filter(4)
    assert cfg.passes_filter(2)
    assert not cfg.passes_filter(1)


def test_load_config_missing_file_returns_default(tmp_path):
    cfg = load_config(tmp_path / "not-exists.yaml")
    assert cfg.min_severity == DEFAULT_CONFIG.min_severity


def test_load_config_from_yaml(tmp_path):
    f = tmp_path / ".ai-review.yaml"
    f.write_text(
        "min_severity: error\nmax_files_per_batch: 5\nreview_focus:\n  - 只关心安全问题\n",
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert cfg.min_severity == 4
    assert cfg.max_files_per_batch == 5
    assert cfg.review_focus == ["只关心安全问题"]


def test_load_config_v2_fields(tmp_path):
    f = tmp_path / ".ai-review.yaml"
    f.write_text(
        "fail_on_severity: warn\n"
        "context_files:\n  - AGENTS.md\n  - spec/**\n"
        "max_context_chars: 4000\n"
        "ignore_generated: false\n",
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert cfg.fail_on_severity == 2
    assert cfg.context_files == ["AGENTS.md", "spec/**"]
    assert cfg.max_context_chars == 4000
    assert cfg.ignore_generated is False


def test_default_v2_fields():
    cfg = DEFAULT_CONFIG
    assert "AGENTS.md" in cfg.context_files
    assert "README.md" in cfg.context_files
    assert cfg.ignore_generated is True
    assert cfg.max_context_chars > 0


def test_load_config_quality_gate(tmp_path):
    f = tmp_path / ".ai-review.yaml"
    f.write_text(
        "quality_gate:\n"
        "  enabled: false\n"
        "  judge_model: deepseek-r1\n"
        "  judge_provider: deepseek\n"
        "  pass_score: 60\n"
        "  max_rewrites: 2\n"
        "  lint_enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_config(f)
    qg = cfg.quality_gate
    assert qg.enabled is False
    assert qg.judge_model == "deepseek-r1"
    assert qg.judge_provider == "deepseek"
    assert qg.pass_score == 60
    assert qg.max_rewrites == 2
    assert qg.lint_enabled is True


def test_default_quality_gate():
    cfg = DEFAULT_CONFIG
    assert cfg.quality_gate.enabled is True
    assert cfg.quality_gate.pass_score == 70
    assert cfg.quality_gate.max_rewrites == 3
    assert cfg.quality_gate.lint_enabled is False  # 首版预留


def test_load_config_warns_when_pyyaml_missing(tmp_path, monkeypatch, caplog):
    """PyYAML 缺失 + 配置文件存在时，不得静默忽略整个配置。

    背景: `if yaml is None: return cfg` 会让 review_focus / min_severity /
    fail_on_severity / context_files 全部退回默认值，且不报错、不警告 ——
    属于「配了但没生效」的静默失败：比配错更危险，因为没有任何信号。
    """
    f = tmp_path / ".ai-review.yaml"
    f.write_text("min_severity: error\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "yaml", None)

    with caplog.at_level(logging.WARNING, logger="pr_review.config"):
        cfg = config_mod.load_config(f)

    # 行为不变: 不抛异常，仍返回可用的默认配置(与"配置文件缺失"的既有约定一致)
    assert cfg.min_severity == 2, "配置读不到时应回落到默认值"
    warnings_logged = [r.getMessage() for r in caplog.records if "PyYAML" in r.getMessage()]
    assert warnings_logged, "必须留下可见的警告，否则用户无从知道配置被忽略了"
    assert str(f) in warnings_logged[0], "警告要指出是哪个文件被忽略了，否则无法排查"


def test_load_config_no_warning_when_no_config_file(tmp_path, monkeypatch, caplog):
    """反向守护: 没给路径 / 路径不存在时不得报警。

    这两种情况是文档明确约定的"使用默认配置"，不是错误。
    没有这条反向断言，上面那条测试可以靠"无脑报警"通过。
    """
    monkeypatch.setattr(config_mod, "yaml", None)

    with caplog.at_level(logging.WARNING, logger="pr_review.config"):
        for arg in (None, tmp_path / "不存在的配置.yaml"):
            cfg = config_mod.load_config(arg)
            assert cfg.min_severity == 2
    assert not [r for r in caplog.records if "PyYAML" in r.getMessage()], (
        "路径缺失属于正常回落，不应报警"
    )
