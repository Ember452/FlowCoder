"""Worktree 变更统计与未推送提交检测。"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@dataclass
class Changes:
    uncommitted: int = 0
    new_commits: int = 0


def count_worktree_changes(wt_path: str, head_commit: str) -> Changes:
    """统计 worktree 相对 head_commit 的未提交改动数与新增提交数。"""
    changes = Changes()
    try:
        status = _run_git(["status", "--porcelain"], cwd=wt_path)
        if status.returncode == 0:
            changes.uncommitted = len([line for line in status.stdout.splitlines() if line.strip()])
    except (subprocess.SubprocessError, OSError):
        # git 不可用时回退为 1（保守视为有改动），避免误删含变更的 worktree
        changes.uncommitted = 1

    try:
        rev_list = _run_git(["rev-list", "--count", f"{head_commit}..HEAD"], cwd=wt_path)
        if rev_list.returncode == 0:
            changes.new_commits = int(rev_list.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        # 与 uncommitted 相同，失败时按有新增提交处理，防止数据丢失
        changes.new_commits = 1

    return changes


def has_worktree_changes(wt_path: str, head_commit: str) -> bool:
    """判断 worktree 相对 head_commit 是否存在任何未提交/未推送的改动。"""
    c = count_worktree_changes(wt_path, head_commit)
    return c.uncommitted > 0 or c.new_commits > 0


@dataclass
class CleanupResult:
    kept: bool
    path: str = ""
    branch: str = ""


def has_unpushed_commits(wt_path: str) -> bool:
    """判断 worktree 是否有尚未推送到任何远程分支的本地提交。"""
    try:
        result = _run_git(
            ["rev-list", "--max-count=1", "HEAD", "--not", "--remotes"],
            cwd=wt_path,
        )
        return bool(result.stdout.strip()) if result.returncode == 0 else True
    except (subprocess.SubprocessError, OSError):
        # 无法确认时保守视为有未推送提交，避免清理掉有远端差异的工作
        return True
