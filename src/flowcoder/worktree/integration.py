"""Worktree 名称生成与提示文案。"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


WORKTREE_NOTICE_TEMPLATE = """\
[WORKTREE CONTEXT]
You have inherited the parent agent's conversation context.
You are currently working in an isolated Git Worktree: {wt_path}
The parent agent's working directory is: {parent_cwd}

IMPORTANT:
- File paths mentioned in the parent conversation refer to the PARENT directory.
- You must translate them to your local worktree path before reading or editing.
- Always re-read files before editing — your copy may differ from the parent's version.
[/WORKTREE CONTEXT]
"""


def generate_worktree_name() -> str:
    """生成不可猜测、低碰撞概率的临时 worktree 名。"""
    # 用 secrets 而非 random：临时名可能出现在路径/分支上，避免可预测/可枚举
    return f"agent-{secrets.token_hex(4)}"


def build_worktree_notice(parent_cwd: str, wt_path: str) -> str:
    # 填充父 agent 工作目录与 worktree 路径，生成注入父上下文的提示文案
    return WORKTREE_NOTICE_TEMPLATE.format(
        parent_cwd=parent_cwd,
        wt_path=wt_path,
    )
