"""创建子 Agent 并补全 trace。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flowcoder.permissions import PermissionMode
from flowcoder.tools.agent.support import (
    create_subagent_permission_checker,
    resolve_parent_trace_id,
)

if TYPE_CHECKING:
    from flowcoder.agent import Agent
    from flowcoder.subagents.parser import AgentDef
    from flowcoder.subagents.trace import TraceManager, TraceNode
    from flowcoder.client import LLMClient
    from flowcoder.tools import ToolRegistry


def create_child_agent(
    *,
    parent_agent: Agent,
    client: LLMClient,
    registry: ToolRegistry,
    work_dir: str,
    definition: AgentDef,
    permission_mode: str | PermissionMode | None = None,
    instructions_content: str | None = None,
    agent_id: str | None = None,
    team_name: str = "",
    team_manager: Any = None,
) -> Agent:
    """创建子 Agent 实例。

    组装一个新的 Agent 供子任务使用：从父 Agent 继承协议 / 上下文窗口 /
    hook 引擎，并按传入的 definition 设定工作目录、最大轮次与权限模式。
    返回时同时把 parent_id / trace_id（及可选的 agent_id、team 信息）挂到子 Agent 上。
    """
    from flowcoder.agent import Agent as AgentClass

    checker = create_subagent_permission_checker(
        work_dir,
        definition.permission_mode if permission_mode is None else permission_mode,
    )
    child = AgentClass(
        client=client,
        registry=registry,
        protocol=parent_agent.protocol,
        work_dir=work_dir,
        max_iterations=definition.max_turns,
        permission_checker=checker,
        context_window=parent_agent.context_window,
        instructions_content=(
            definition.system_prompt if instructions_content is None else instructions_content
        ),
        hook_engine=parent_agent.hook_engine,
    )
    child.parent_id = parent_agent.agent_id
    child.trace_id = resolve_parent_trace_id(parent_agent)
    if agent_id is not None:
        child.agent_id = agent_id
    if team_name:
        child.team_name = team_name
    if team_manager is not None:
        child._team_manager = team_manager
    return child


def complete_trace_from_agent(
    trace_manager: TraceManager,
    trace_node: TraceNode,
    agent: Agent,
    *,
    status: str = "completed",
) -> None:
    """回写子 Agent 的 token 统计并结束其 trace 节点。"""
    trace_manager.update(
        trace_node.agent_id,
        input_tokens=agent.total_input_tokens,
        output_tokens=agent.total_output_tokens,
    )
    trace_manager.complete(trace_node.agent_id, status)
