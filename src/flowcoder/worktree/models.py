"""Worktree / WorktreeSession 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Worktree:
    """已创建/已恢复的 git worktree 的元信息。"""

    name: str
    path: str
    branch: str
    based_on: str
    head_commit: str
    created: datetime = field(default_factory=datetime.now)


@dataclass
class WorktreeSession:
    """进入 worktree 前的现场快照，用于退出/恢复时还原主工作区状态。"""

    original_cwd: str
    worktree_path: str
    worktree_name: str
    original_branch: str
    original_head_commit: str
    session_id: str = ""
    hook_based: bool = False
