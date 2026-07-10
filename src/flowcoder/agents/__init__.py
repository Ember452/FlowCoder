"""子 Agent / 多智能体定义与运行支持包。"""

from flowcoder.agents.parser import AgentDef, AgentParseError, parse_agent_file
from flowcoder.agents.loader import AgentLoader
from flowcoder.agents.tool_filter import resolve_agent_tools
from flowcoder.agents.fork import build_forked_messages, ForkError
from flowcoder.agents.trace import TraceManager, TraceNode
from flowcoder.agents.task_manager import TaskManager, BackgroundTask
from flowcoder.agents.notification import format_task_notification, inject_task_notifications


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
