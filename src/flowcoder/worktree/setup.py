"""新建 worktree 后的配置拷贝与 hook/symlink 设置。"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

LOCAL_CONFIG_FILES = [
    "settings.local.json",
    ".env",
]


def perform_post_creation_setup(
    repo_root: str,
    wt_path: str,
    symlink_directories: list[str] | None = None,
) -> None:
    """新建 worktree 后补齐主仓库的本地配置、hooks、symlink 与被忽略文件。"""
    root = Path(repo_root)
    wt = Path(wt_path)

    _copy_local_configs(root, wt)
    _setup_git_hooks(root, wt)
    _create_symlinks(root, wt, symlink_directories or [])
    _copy_ignored_files(root, wt)


def _copy_local_configs(root: Path, wt: Path) -> None:
    # 把仅存在于本机、不入库的配置文件（如本地 env）复制进 worktree 以保持行为一致
    for name in LOCAL_CONFIG_FILES:
        src = root / name
        if src.exists():
            dst = wt / name
            try:
                shutil.copy2(str(src), str(dst))
                log.debug("Copied %s to worktree", name)
            except OSError as e:
                log.warning("Failed to copy %s: %s", name, e)


def _setup_git_hooks(root: Path, wt: Path) -> None:
    # 让 worktree 复用一个 hooks 目录（husky 或 .git/hooks），保证提交钩子在新工作区同样生效
    hooks_path: str | None = None

    husky_dir = root / ".husky"
    if husky_dir.is_dir():
        hooks_path = str(husky_dir)
    else:
        git_hooks = root / ".git" / "hooks"
        if git_hooks.is_dir():
            hooks_path = str(git_hooks)

    if hooks_path is None:
        return

    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", hooks_path],
            cwd=str(wt),
            capture_output=True,
            timeout=10,
        )
        log.debug("Set core.hooksPath to %s in worktree", hooks_path)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Failed to set hooks path: %s", e)


def _create_symlinks(root: Path, wt: Path, directories: list[str]) -> None:
    # 对大型/易变的目录建立指向主仓库的 symlink，避免复制占用磁盘且难同步（src 已存在再跳过）
    for dirname in directories:
        src = root / dirname
        dst = wt / dirname
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            continue
        try:
            os.symlink(str(src), str(dst))
            log.debug("Symlinked %s to worktree", dirname)
        except OSError as e:
            log.warning("Failed to symlink %s: %s", dirname, e)


def _copy_ignored_files(root: Path, wt: Path) -> None:
    # .worktreeinclude 列出需要额外复制进 worktree 的忽略文件（默认 git 不会带到新工作区）
    include_file = root / ".worktreeinclude"
    if not include_file.exists():
        return

    try:
        patterns = [
            line.strip()
            for line in include_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return

    if not patterns:
        return

    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return
        ignored_files = [f.rstrip("/") for f in result.stdout.splitlines() if f.strip()]
    except (subprocess.SubprocessError, OSError):
        return

    for rel_path in ignored_files:
        if not any(fnmatch.fnmatch(rel_path, pat) for pat in patterns):
            continue
        src = root / rel_path
        dst = wt / rel_path
        if not src.is_file():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            log.debug("Copied ignored file %s to worktree", rel_path)
        except OSError as e:
            log.warning("Failed to copy ignored file %s: %s", rel_path, e)
