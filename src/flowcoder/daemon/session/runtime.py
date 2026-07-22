"""DaemonSessionRuntime 创建与持有。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from flowcoder.agent import Agent
from flowcoder.agent.factory import AgentDeps, create_agent_from_config
from flowcoder.config import AppConfig
from flowcoder.conversation import ConversationManager
from flowcoder.daemon.session import SessionManager
from flowcoder.hooks import HookEngine
from flowcoder.permissions import PermissionMode


@dataclass(frozen=True)
class DaemonSessionRuntime:
    agent: Agent
    deps: AgentDeps
    conversation: ConversationManager


AgentFactory = Callable[
    [AppConfig, str, PermissionMode, HookEngine | None],
    Awaitable[tuple[Agent, AgentDeps]],
]


async def create_daemon_session_runtime(
    *,
    sid: str,
    config: AppConfig,
    work_dir: str,
    permission_mode: PermissionMode,
    hook_engine: HookEngine | None,
    session_mgr: SessionManager,
    agent_factory: AgentFactory = create_agent_from_config,
    register_session: bool = True,
    conversation: ConversationManager | None = None,
) -> DaemonSessionRuntime:
    agent, deps = await agent_factory(
        config,
        work_dir,
        permission_mode,
        hook_engine,
    )
    agent.session_id = sid
    conversation = conversation or ConversationManager()
    runtime = DaemonSessionRuntime(agent, deps, conversation)
    if register_session:
        await session_mgr.create_session(sid, agent, conversation)
    return runtime
