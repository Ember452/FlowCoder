"""多 Agent 团队协作包。"""

from flowcoder.teams.mailbox import Mailbox, MailboxMessage, create_message
from flowcoder.teams.models import (
    AgentTeam,
    BackendType,
    TeammateInfo,
    resolve_team_dir,
    unique_team_name,
)
from flowcoder.teams.progress import TeammateProgress, ToolActivity
from flowcoder.teams.registry import AgentNameRegistry
from flowcoder.teams.shared_task import SharedTask, SharedTaskStore


__all__ = [
    "AgentTeam",
    "AgentNameRegistry",
    "BackendType",
    "Mailbox",
    "MailboxMessage",
    "SharedTask",
    "SharedTaskStore",
    "TeammateInfo",
    "TeammateProgress",
    "ToolActivity",
    "create_message",
    "resolve_team_dir",
    "unique_team_name",
]
