"""git 操作封装(subprocess): 建分支 / 应用快照 / 提交 / 推送 / 删分支。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitOps:
    def __init__(self, repo_dir: Path, dry_run: bool = False):
        self.repo_dir = repo_dir
        self.dry_run = dry_run

    def _run(self, *args: str, check: bool = True) -> str:
        cmd = ["git", "-C", str(self.repo_dir), *args]
        if self.dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
            return ""
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
        return proc.stdout.strip()

    # ------------------------------------------------------------------ 流程步骤
    def checkout_main(self) -> None:
        self._run("checkout", "main")
        self._run("pull", "origin", "main", check=False)

    def new_branch(self, name: str) -> None:
        self._run("checkout", "-b", name)

    def apply_snapshot(self, src: Path) -> None:
        """把快照目录内容覆盖到仓库工作区(镜像路径)。"""
        if self.dry_run:
            print(f"[dry-run] 应用快照 {src}")
            return
        for item in src.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(src)
            dest = self.repo_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)

    def commit(self, msg: str) -> None:
        self._run("add", "-A")
        self._run("commit", "-m", msg, check=False)  # 允许空提交(无变更时)

    def push(self, branch: str) -> None:
        # 网络时好时坏(2026-08-14 回归根因): push 失败重试 3 次再抛
        last_err: RuntimeError | None = None
        for attempt in range(4):
            try:
                self._run("push", "-u", "origin", branch)
                return
            except RuntimeError as e:
                last_err = e
                print(f"  push {branch} 失败(第{attempt + 1}/4 次): {str(e)[:120]}, 重试...")
                import time

                time.sleep(4)
        raise last_err  # type: ignore[misc]

    def delete_remote_branch(self, branch: str) -> None:
        # 直接 subprocess 拿 stderr: 失败要可见(否则残留分支会挡下次 push)
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.repo_dir), "push", "origin", "--delete", branch],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"  [warn] 远程分支 {branch} 清理失败(稍后手动删): {proc.stderr.strip()[:120]}")

    def checkout_and_cleanup(self, branch: str) -> None:
        self._run("checkout", "main")
        self._run("branch", "-D", branch, check=False)
