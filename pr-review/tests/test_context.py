"""ContextCollector 仓库上下文收集单测。"""

from pr_review.context import ContextCollector


def _make_repo(tmp_path):
    """造一个带 AGENTS.md/README/spec 的小仓库。"""
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Guidelines\n\n- 遵循 Conventional Commits\n- 后端用 Go + Gin\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# devops-dashboard\n运维监控仪表盘", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "conventions.md").write_text("# 约定\n分层 Controller/Service", encoding="utf-8")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "v1-api.yaml").write_text(
        "openapi: 3.0.0\npaths:\n  /api/settings/webhook:\n    get: ...",
        encoding="utf-8",
    )
    return tmp_path


def test_collect_globs_in_order(tmp_path):
    repo = _make_repo(tmp_path)
    collector = ContextCollector(
        repo,
        patterns=["AGENTS.md", "README.md", "docs/**/*.md", "spec/**"],
    )
    text = collector.collect()
    assert "Agent Guidelines" in text
    assert "devops-dashboard" in text
    assert "分层 Controller/Service" in text
    assert "openapi: 3.0.0" in text
    # 顺序: AGENTS 在最前
    assert text.index("Agent Guidelines") < text.index("openapi")


def test_collect_missing_files_silently_skipped(tmp_path):
    collector = ContextCollector(tmp_path, patterns=["AGENTS.md", "not-exist.md"])
    text = collector.collect()
    assert text == ""


def test_collect_empty_repo(tmp_path):
    assert ContextCollector(tmp_path, patterns=["AGENTS.md"]).collect() == ""


def test_collect_respects_max_chars(tmp_path):
    repo = _make_repo(tmp_path)
    collector = ContextCollector(
        repo,
        patterns=["AGENTS.md", "README.md"],
        max_chars=100,
    )
    text = collector.collect()
    assert len(text) <= 100 + 100  # 允许最后一个块截断误差


def test_collect_dedup_patterns(tmp_path):
    repo = _make_repo(tmp_path)
    collector = ContextCollector(repo, patterns=["AGENTS.md", "AGENTS.md"])
    text = collector.collect()
    assert text.count("Agent Guidelines") == 1


def test_head_limit_only_reads_first_lines(tmp_path):
    repo = _make_repo(tmp_path)
    long = "\n".join(f"line{i}" for i in range(500))
    (tmp_path / "README.md").write_text(long, encoding="utf-8")
    collector = ContextCollector(repo, patterns=["README.md"])
    text = collector.collect()
    assert "line0" in text
    assert "line499" not in text  # 只读前 150 行
