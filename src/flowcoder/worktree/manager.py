"""WorktreeManager：创建、列表、删除 worktree。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

from flowcoder.worktree.changes import (
    CleanupResult,
    count_worktree_changes,
    has_worktree_changes,
)
from flowcoder.worktree.models import Worktree, WorktreeSession
from flowcoder.worktree.session import load_worktree_session, save_worktree_session
from flowcoder.worktree.setup import perform_post_creation_setup
from flowcoder.worktree.slug import flatten_slug, validate_slug

log = logging.getLogger(__name__)

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
VALID_EXIT_ACTIONS = {"keep", "remove"}
_GIT_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class WorktreeError(Exception):
    pass


def _normalized_git_object_id(value: str) -> str | None:
    """校验并规范化 git 对象 ID（40/64 位十六进制），非法返回 None。"""
    candidate = value.strip()
    if not _GIT_OBJECT_ID_RE.fullmatch(candidate):
        return None
    # git 对象 ID 不区分大小写，统一小写便于跨来源（文件/命令输出）比较
    return candidate.lower()


class WorktreeManager:
    def __init__(
        self,
        repo_root: str,
        symlink_directories: list[str] | None = None,
        worktree_dir: str | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.symlink_directories = symlink_directories or []
        self.worktree_dir = worktree_dir or str(Path(repo_root) / ".flowcoder" / "worktrees")
        self._flowcoder_dir = Path(repo_root) / ".flowcoder"
        self._lock = asyncio.Lock()
        self.active: dict[str, Worktree] = {}
        self.current_session: WorktreeSession | None = None

    def _run_git(self, args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        # GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS="" 强制 git 禁用交互式认证/提示，
        # 保证在非交互环境（如后台任务）中遇到认证需求时立即失败而非挂起
        env = {**os.environ, **GIT_ENV}
        return subprocess.run(
            ["git"] + args,
            cwd=cwd or self.repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=env,
        )

    # ------------------------------------------------------------------
    # 快速恢复：直接从文件系统读取 HEAD SHA，无需启动 git 子进程
    # ------------------------------------------------------------------

    @staticmethod
    def read_worktree_head_sha(wt_path: str) -> str | None:
        """直接解析 .git 站点文件读取当前 HEAD 提交 ID，避免启动 git 子进程（快速恢复路径）。"""
        wt = Path(wt_path)
        git_file = wt / ".git"
        if not git_file.exists():
            return None

        try:
            content = git_file.read_text(encoding="utf-8").strip()
            if not content.startswith("gitdir:"):
                return None
            gitdir = Path(content.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (wt / gitdir).resolve()

            commondir_file = gitdir / "commondir"
            if commondir_file.exists():
                commondir_rel = commondir_file.read_text(encoding="utf-8").strip()
                commondir = (gitdir / commondir_rel).resolve()
            else:
                commondir = gitdir

            head_file = gitdir / "HEAD"
            if not head_file.exists():
                return None
            head_content = head_file.read_text(encoding="utf-8").strip()

            if head_content.startswith("ref:"):
                ref_path = head_content.split(":", 1)[1].strip()
                ref_file = gitdir / ref_path
                if not ref_file.exists():
                    ref_file = commondir / ref_path
                if ref_file.exists():
                    return _normalized_git_object_id(ref_file.read_text(encoding="utf-8"))
                packed_refs = commondir / "packed-refs"
                if packed_refs.exists():
                    for line in packed_refs.read_text(encoding="utf-8").splitlines():
                        if line.strip() and not line.startswith("#"):
                            parts = line.split()
                            if len(parts) == 2 and parts[1] == ref_path:
                                return _normalized_git_object_id(parts[0])
                return None
            return _normalized_git_object_id(head_content)
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 创建 worktree
    # ------------------------------------------------------------------

    async def create(self, name: str, base_branch: str = "HEAD") -> Worktree:
        """新建 worktree；若同名目录已存在则直接复用（快速恢复），否则走 git worktree add。"""
        async with self._lock:
            err = validate_slug(name)
            if err:
                raise WorktreeError(err)

            if name in self.active:
                raise WorktreeError(f"worktree already exists: {name}")

            # 斜杠被扁平化为 +，保证磁盘路径为单段，避免嵌套目录
            flat_slug = flatten_slug(name)
            wt_path = os.path.join(self.worktree_dir, flat_slug)
            branch_name = f"worktree-{flat_slug}"

            head_sha = self.read_worktree_head_sha(wt_path)
            if head_sha is not None:
                log.info("Fast recovery: reusing existing worktree at %s", wt_path)
                wt = Worktree(
                    name=name,
                    path=wt_path,
                    branch=branch_name,
                    based_on=base_branch,
                    head_commit=head_sha,
                )
                self.active[name] = wt
                return wt

            os.makedirs(self.worktree_dir, exist_ok=True)

            result = self._run_git(
                [
                    "worktree",
                    "add",
                    "-B",
                    branch_name,
                    wt_path,
                    base_branch,
                ]
            )
            if result.returncode != 0:
                raise WorktreeError(f"git worktree add failed: {result.stderr.strip()}")

            perform_post_creation_setup(
                self.repo_root,
                wt_path,
                symlink_directories=self.symlink_directories,
            )

            head_sha = self.read_worktree_head_sha(wt_path) or ""
            wt = Worktree(
                name=name,
                path=wt_path,
                branch=branch_name,
                based_on=base_branch,
                head_commit=head_sha,
            )
            self.active[name] = wt
            return wt

    # ------------------------------------------------------------------
    # 进入 worktree
    # ------------------------------------------------------------------

    async def enter(self, name: str) -> WorktreeSession:
        """进入 worktree，记录进入前的分支/HEAD 并把会话落盘，供退出时恢复。"""
        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")

        original_branch = self._get_current_branch()
        original_head = self._get_head_commit()

        session = WorktreeSession(
            original_cwd=self.repo_root,
            worktree_path=wt.path,
            worktree_name=name,
            original_branch=original_branch,
            original_head_commit=original_head,
        )
        self.current_session = session
        save_worktree_session(self._flowcoder_dir, session)
        return session

    # ------------------------------------------------------------------
    # 退出 worktree
    # ------------------------------------------------------------------

    async def exit(
        self,
        name: str,
        action: str = "keep",
        discard_changes: bool = False,
    ) -> None:
        """退出 worktree；action=remove 时删除 worktree 与其分支，有未提交变更时需 discard_changes 强制。"""
        if action not in VALID_EXIT_ACTIONS:
            raise WorktreeError(f"invalid worktree exit action: {action}")

        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")

        session = self.current_session
        if session is None:
            raise WorktreeError("not in a worktree")
        if session.worktree_name != name:
            raise WorktreeError(f"not in worktree: {name}")

        if action == "remove" and not discard_changes:
            changes = count_worktree_changes(wt.path, wt.head_commit)
            if changes.uncommitted > 0 or changes.new_commits > 0:
                raise WorktreeError(
                    f"worktree has changes ({changes.uncommitted} uncommitted, "
                    f"{changes.new_commits} new commits). "
                    "Set discard_changes=True to force removal."
                )

        self.current_session = None
        save_worktree_session(self._flowcoder_dir, None)

        if action == "remove":
            await self._remove_worktree(name, wt)

    # ------------------------------------------------------------------
    # 删除 worktree（内部方法）
    # ------------------------------------------------------------------

    async def _remove_worktree(self, name: str, wt: Worktree) -> None:
        result = self._run_git(["worktree", "remove", "--force", wt.path])
        if result.returncode != 0:
            log.warning("git worktree remove failed: %s", result.stderr.strip())

        # 短暂让出事件循环，等 git 落盘完成后删除分支，避免竞态失败
        await asyncio.sleep(0.1)

        flat_slug = flatten_slug(name)
        branch_name = f"worktree-{flat_slug}"
        self._run_git(["branch", "-D", branch_name])

        self.active.pop(name, None)

    # ------------------------------------------------------------------
    # 自动清理
    # ------------------------------------------------------------------

    async def auto_cleanup(self, name: str, head_commit: str) -> CleanupResult:
        """按 head_commit 检测 worktree 是否有变更：无变更则自动删除，有变更则保留。"""
        wt = self.active.get(name)
        if wt is None:
            return CleanupResult(kept=False)

        if has_worktree_changes(wt.path, head_commit):
            return CleanupResult(kept=True, path=wt.path, branch=wt.branch)

        await self._remove_worktree(name, wt)
        return CleanupResult(kept=False)

    # ------------------------------------------------------------------
    # 列出 / 查询
    # ------------------------------------------------------------------

    def list_worktrees(self) -> list[Worktree]:
        return list(self.active.values())

    def get_current_session(self) -> WorktreeSession | None:
        return self.current_session

    def _restored_worktree_path(self, session: WorktreeSession) -> Path | None:
        """校验持久化会话指向的 worktree 是否可安全恢复，返回其路径（非法则 None）。"""
        err = validate_slug(session.worktree_name)
        if err:
            log.warning(
                "Ignoring persisted worktree session with invalid name %r: %s",
                session.worktree_name,
                err,
            )
            return None

        managed_root = Path(self.worktree_dir).resolve()
        wt_path = Path(session.worktree_path).resolve()
        # 要求 worktree 路径必须位于受管目录内，防止被篡改的会话指向任意路径（路径逃逸）
        try:
            wt_path.relative_to(managed_root)
        except ValueError:
            log.warning(
                "Ignoring persisted worktree outside managed directory: %s",
                wt_path,
            )
            return None
        return wt_path

    # ------------------------------------------------------------------
    # 从持久化的 session 中恢复
    # ------------------------------------------------------------------

    def restore_session(self) -> WorktreeSession | None:
        """从磁盘恢复上一次持久化的 worktree 会话（重启后重新进入该 worktree）。"""
        session = load_worktree_session(self._flowcoder_dir)
        if session is None:
            return None
        wt_path = self._restored_worktree_path(session)
        if wt_path is None:
            save_worktree_session(self._flowcoder_dir, None)
            return None

        head_sha = self.read_worktree_head_sha(str(wt_path))
        if head_sha is None:
            save_worktree_session(self._flowcoder_dir, None)
            return None

        wt = Worktree(
            name=session.worktree_name,
            path=str(wt_path),
            branch=f"worktree-{flatten_slug(session.worktree_name)}",
            based_on="unknown",
            head_commit=head_sha,
        )
        self.active[session.worktree_name] = wt
        self.current_session = session
        return session

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_current_branch(self) -> str:
        # 当前不在具体分支（如分离头）或 git 调用失败时回退为 "HEAD" 兜底，绝不抛错
        try:
            result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            return result.stdout.strip() if result.returncode == 0 else "HEAD"
        except (subprocess.SubprocessError, OSError):
            return "HEAD"

    def _get_head_commit(self) -> str:
        # 读取失败时回退为空串，调用方（enter）据此容忍"未知起点"
        try:
            result = self._run_git(["rev-parse", "HEAD"])
            return result.stdout.strip() if result.returncode == 0 else ""
        except (subprocess.SubprocessError, OSError):
            return ""
