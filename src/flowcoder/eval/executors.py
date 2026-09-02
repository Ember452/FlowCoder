"""评测执行通道的真实实现（P5 重构拆分，行为零变化）。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from flowcoder.eval.models import (
    AgentSolution,
    ExecutionOutcome,
    SolverSession,
)

logger = logging.getLogger(__name__)


class _LiveSession:
    """会话式驱动真实 Agent 循环：同一会话内多轮 ask（自愈修复共用上下文）。"""

    def __init__(self, agent: object) -> None:
        from flowcoder.conversation import ConversationManager

        self._agent = agent
        self._conversation = ConversationManager()

    async def ask(self, prompt: str) -> AgentSolution:
        """驱动 Agent 奔跑一轮，聚合文本输出与 token/轮次等统计到 AgentSolution。"""
        from flowcoder.agent import ErrorEvent, LoopComplete, StreamText, UsageEvent

        solution = AgentSolution()
        self._conversation.add_user_message(prompt)
        async for event in self._agent.run(self._conversation):
            if isinstance(event, StreamText):
                solution.text += event.text
            elif isinstance(event, UsageEvent):
                solution.input_tokens += event.input_tokens
                solution.output_tokens += event.output_tokens
            elif isinstance(event, ErrorEvent):
                solution.error = event.message
            elif isinstance(event, LoopComplete):
                solution.turns = event.total_turns
        return solution


class LiveAgentSolver:
    """真实 Agent 的 Solver：每个会话经 agent_factory 创建独立 Agent 实例。

    k-sample 的 trial 并行运行，Agent 实例携带可变状态（recovery_state、
    token 计数等），不能共享——工厂按会话建实例，client/registry 可复用。
    """

    def __init__(self, agent_factory: Callable[[], object]) -> None:
        self._agent_factory = agent_factory

    def start(self) -> SolverSession:
        return _LiveSession(self._agent_factory())


class DockerSandboxExecutor:
    """基于 sandbox 模块容器池的执行器（真实 Docker 路径）。"""

    def __init__(self, *, image: str = "python:3.11-slim", pool_size: int = 2) -> None:
        from flowcoder.sandbox import SandboxConfig

        self._config = SandboxConfig(image=image)
        self._pool_size = pool_size
        self._pool = None

    async def _ensure_pool(self):
        from flowcoder.sandbox import SandboxPool

        if self._pool is None:
            pool = SandboxPool(size=self._pool_size, config=self._config)
            await pool.start()
            self._pool = pool
        return self._pool

    async def run_test(self, files: Mapping[str, str], timeout_s: float) -> ExecutionOutcome:
        """把待测文件写入沙箱并执行 run_test.py，映射 sandbox 结果为 ExecutionOutcome。"""
        from flowcoder.sandbox import SandboxError

        try:
            pool = await self._ensure_pool()
            result = await pool.execute(
                ["python", "run_test.py"], files=dict(files), timeout_s=timeout_s
            )
        except SandboxError as e:
            raise RuntimeError(f"沙箱不可用：{e}") from e
        return ExecutionOutcome(
            exit_code=result.exit_code if result.exit_code is not None else -1,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )
