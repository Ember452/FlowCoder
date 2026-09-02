"""清理过期/临时 worktree。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from flowcoder.worktree.changes import has_unpushed_commits, has_worktree_changes
from flowcoder.worktree.manager import WorktreeManager

log = logging.getLogger(__name__)

# 识别"临时" worktree 的名字模式：若程序崩溃留下孤儿目录，仅符合这些模式才会被自动清理，
# 用户自定义的持久 worktree 不受影响
EPHEMERAL_PATTERNS = [
    re.compile(r"^agent-a[0-9a-f]{7}$"),
    re.compile(r"^wf_[0-9a-f]{8}-[0-9a-f]{3}-\d+$"),
    re.compile(r"^wf-\d+$"),
    re.compile(r"^bridge-[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$"),
    re.compile(r"^job-[a-zA-Z0-9._-]{1,55}-[0-9a-f]{8}$"),
]


def _is_ephemeral(name: str) -> bool:
    """判断 worktree 名是否命中"临时"模式，只有这类目录才在超时后被自动清理。"""
    return any(p.match(name) for p in EPHEMERAL_PATTERNS)


async def cleanup_stale_worktrees(manager: WorktreeManager, cutoff_hours: int) -> int:
    """清理超出存活时长、无任何未提交/未推送改动的临时 worktree，返回清理数量。"""
    cutoff = datetime.now() - timedelta(hours=cutoff_hours)
    removed = 0
    worktree_dir = Path(manager.worktree_dir)

    if not worktree_dir.exists():
        return 0

    for entry in worktree_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name

        if not _is_ephemeral(name):
            continue

        if manager.current_session and manager.current_session.worktree_name == name:
            continue

        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime > cutoff:
                continue
        except OSError:
            continue

        head_sha = WorktreeManager.read_worktree_head_sha(str(entry))
        if head_sha is None:
            continue

        if has_worktree_changes(str(entry), head_sha):
            continue

        if has_unpushed_commits(str(entry)):
            continue

        try:
            flat_name = name
            if flat_name in manager.active:
                await manager._remove_worktree(flat_name, manager.active[flat_name])
            else:
                result = manager._run_git(["worktree", "remove", "--force", str(entry)])
                if result.returncode == 0:
                    await asyncio.sleep(0.1)
                    manager._run_git(["branch", "-D", f"worktree-{flat_name}"])
            removed += 1
            log.info("Cleaned up stale worktree: %s", name)
        except Exception as e:
            log.warning("Failed to clean up stale worktree %s: %s", name, e)

    return removed


async def start_stale_cleanup_task(
    manager: WorktreeManager,
    interval: int,
    cutoff_hours: int,
) -> None:
    """后台循环：每隔 interval 秒执行一次过期 worktree 清理，直至所在任务被取消。"""
    while True:
        # 先 sleep 再清理，避免启动瞬间立即扫描；首轮清理被推迟一个周期
        await asyncio.sleep(interval)
        try:
            count = await cleanup_stale_worktrees(manager, cutoff_hours)
            if count:
                log.info("Stale worktree cleanup removed %d worktrees", count)
        except Exception as e:
            log.warning("Stale worktree cleanup error: %s", e)
