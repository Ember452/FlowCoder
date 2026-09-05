"""子 Agent / 多智能体定义与运行支持包。"""

from flowcoder.subagents.parser import AgentDef, AgentParseError, parse_agent_file
from flowcoder.subagents.loader import AgentLoader
from flowcoder.subagents.tool_filter import resolve_agent_tools
from flowcoder.subagents.fork import build_forked_messages, ForkError
from flowcoder.subagents.trace import TraceManager, TraceNode
from flowcoder.subagents.task_manager import TaskManager, BackgroundTask
from flowcoder.subagents.notification import format_task_notification, inject_task_notifications


__all__ = [
    "AgentDef",
    "AgentParseError",
    "parse_agent_file",
    "AgentLoader",
    "resolve_agent_tools",
    "build_forked_messages",
    "ForkError",
    "TraceManager",
    "TraceNode",
    "TaskManager",
    "BackgroundTask",
    "format_task_notification",
    "inject_task_notifications",
]
