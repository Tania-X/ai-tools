"""LocalPlatform 测试: 不依赖 GitHub, 只依赖本地 git 仓库。"""

from __future__ import annotations

import subprocess

from pr_review.local_platform import LocalPlatform
from pr_review.models import PRFile


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


def _commit(repo, message):
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _make_diverged_repo(tmp_path):
    """造一个"分支基于较旧 base、base 之后又新增文件"的仓库(评审 R3-1 的现场)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "a.py").write_text("v1\n", encoding="utf-8")
    _commit(repo, "init")
    _git(repo, "checkout", "-q", "-b", "feature")      # 分支从旧 base 切出
    (repo / "feature.py").write_text("print('f')\n", encoding="utf-8")
    (repo / "a.py").write_text("v1\nfeature-only\n", encoding="utf-8")
    _commit(repo, "feature work")
    _git(repo, "checkout", "-q", "main")               # base 之后又合入了新文件与改动
    (repo / "report.md").write_text("报告\n", encoding="utf-8")
    (repo / "a.py").write_text("v1\nmain-only\n", encoding="utf-8")
    _commit(repo, "main moves ahead")
    return repo


def test_local_platform_uses_merge_base_not_raw_two_dot(tmp_path):
    """回归(评审 R3-1): 本地 diff 必须用三点(merge-base), 否则 base 上的新文件会被当成"本分支删除"。

    这条件不是洁癖: 误报就是这么来的——本地跑 PR #3 评审时, base(main)上新合入的报告文件
    被算作"本 PR 删除", 于是产出一条"文档死链"的误报, 而 GitHub 的 PR 视图里根本没有这个变更。
    """
    repo = _make_diverged_repo(tmp_path)
    files = LocalPlatform(repo, base="main", head="feature").get_pr_files()
    names = {f.filename for f in files}
    # 本分支真正改过的(新增 + 修改)才有, base 上独有的文件不该出现
    assert names == {"feature.py", "a.py"}, f"三点 diff 只应包含本分支的改动, 实际 {names}"
    # 两点 diff 会把 base 上的 report.md 也算进来(这就是要避免的幻影删除)
    raw_two_dot = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-status", "main", "feature"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "report.md" in raw_two_dot and "report.md" not in names

    # patch 也必须是三点: 两点会把 base 上的新改动显示成"本分支删除"(幻影回退)
    patch = next(f.patch for f in files if f.filename == "a.py")
    assert "feature-only" in patch
    assert "main-only" not in patch, f"patch 用了两点 diff, 出现幻影回退:\n{patch}"


def test_local_platform_three_dot_still_reports_real_deletion(tmp_path):
    """反向守卫: 本分支真的删了文件, 三点 diff 仍然要报出来(不能修成"什么都不删")。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "b.py").write_text("print(2)\n", encoding="utf-8")
    _commit(repo, "init")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.py").unlink()
    _commit(repo, "remove b")

    files = LocalPlatform(repo, base="main", head="feature").get_pr_files()
    assert [f.filename for f in files] == ["b.py"]
    assert files[0].status.startswith("D")
