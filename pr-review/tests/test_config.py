"""审查配置加载 / 过滤逻辑单测。"""

import pytest

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
        "  pass_score: 60\n"
        "  max_rewrites: 2\n"
        "  lint_enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_config(f)
    qg = cfg.quality_gate
    assert qg.enabled is False
    assert qg.judge_model == "deepseek-r1"
    assert qg.pass_score == 60
    assert qg.max_rewrites == 2
    assert qg.lint_enabled is True


def test_default_quality_gate():
    cfg = DEFAULT_CONFIG
    assert cfg.quality_gate.enabled is True
    assert cfg.quality_gate.pass_score == 70
    assert cfg.quality_gate.max_rewrites == 3
    assert cfg.quality_gate.lint_enabled is False  # 首版预留
