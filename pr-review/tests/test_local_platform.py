"""LocalPlatform 测试: 不依赖 GitHub, 只依赖本地 git 仓库。"""

from __future__ import annotations

import subprocess

from pr_review.local_platform import LocalPlatform
from pr_review.types import PRFile


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_local_platform_reads_git_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    (repo / "a.py").write_text("print(2)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change")

    platform = LocalPlatform(repo)
    pr = platform.get_pr_info()
    assert pr.head_sha

    files = platform.get_pr_files()
    assert len(files) == 1
    assert isinstance(files[0], PRFile)
    assert files[0].filename == "a.py"
    assert "print(2)" in files[0].patch
