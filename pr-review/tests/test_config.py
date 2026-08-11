"""审查配置加载 / 过滤逻辑单测。"""

import pytest

from pr_review.config import DEFAULT_CONFIG, SEVERITIES, load_config


def test_default_config_sane():
    cfg = DEFAULT_CONFIG
    assert cfg.min_severity in SEVERITIES
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
    assert cfg.passes_filter("error")
    assert cfg.passes_filter("warn")
    assert not cfg.passes_filter("info")


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
    assert cfg.min_severity == "error"
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
    assert cfg.fail_on_severity == "warn"
    assert cfg.context_files == ["AGENTS.md", "spec/**"]
    assert cfg.max_context_chars == 4000
    assert cfg.ignore_generated is False


def test_default_v2_fields():
    cfg = DEFAULT_CONFIG
    assert "AGENTS.md" in cfg.context_files
    assert "README.md" in cfg.context_files
    assert cfg.ignore_generated is True
    assert cfg.max_context_chars > 0
