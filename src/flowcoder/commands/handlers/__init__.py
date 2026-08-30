"""内置斜杠命令注册入口。"""

from __future__ import annotations

from flowcoder.commands.handlers.account import ACCOUNT_COMMAND
from flowcoder.commands.handlers.clear import CLEAR_COMMAND
from flowcoder.commands.handlers.compact import COMPACT_COMMAND
from flowcoder.commands.handlers.do import DO_COMMAND
from flowcoder.commands.handlers.help import HELP_COMMAND
from flowcoder.commands.handlers.mcp import MCP_COMMAND
from flowcoder.commands.handlers.memory import MEMORY_COMMAND
from flowcoder.commands.handlers.permission import PERMISSION_COMMAND
from flowcoder.commands.handlers.plan import PLAN_COMMAND
from flowcoder.commands.handlers.review import REVIEW_COMMAND
from flowcoder.commands.handlers.session import SESSION_COMMAND
from flowcoder.commands.handlers.skill import SKILL_COMMAND
from flowcoder.commands.handlers.rewind import REWIND_COMMAND
from flowcoder.commands.handlers.sandbox import SANDBOX_COMMAND
from flowcoder.commands.handlers.status import STATUS_COMMAND
from flowcoder.commands.registry import CommandRegistry


ALL_COMMANDS = [
    HELP_COMMAND,
    ACCOUNT_COMMAND,
    COMPACT_COMMAND,
    CLEAR_COMMAND,
    PLAN_COMMAND,
    REVIEW_COMMAND,
    DO_COMMAND,
    SESSION_COMMAND,
    MCP_COMMAND,
    MEMORY_COMMAND,
    PERMISSION_COMMAND,
    REWIND_COMMAND,
    SANDBOX_COMMAND,
    STATUS_COMMAND,
    SKILL_COMMAND,
]


def register_all_commands(registry: CommandRegistry) -> None:
    for cmd in ALL_COMMANDS:
        registry.register_sync(cmd)
