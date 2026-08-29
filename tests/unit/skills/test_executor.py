"""SkillExecutor fork 执行的权限边界测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from pydantic import BaseModel

from flowcoder.agent import Agent
from flowcoder.client import LLMClient
from flowcoder.conversation import ConversationManager
from flowcoder.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from flowcoder.skills.executor import SkillExecutor
from flowcoder.skills.parser import SkillDef
from flowcoder.tools import ToolRegistry
from flowcoder.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)


class MockLLMClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = list(responses)
        self._call_index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._call_index >= len(self._responses):
            yield TextDelta(text="No more responses")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
            return
        events = self._responses[self._call_index]
        self._call_index += 1
        for e in events:
            yield e


class _BashParams(BaseModel):
    command: str = ""


class RecordingBashTool(Tool):
    """category=command 的工具：DEFAULT 模式下 Benign 命令也会走到 ask 决策。"""

    name = "Bash"
    description = "test command tool"
    category = "command"
    params_model = _BashParams

    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    async def execute(self, params: BaseModel) -> ToolResult:
        self.executed.append(getattr(params, "command", ""))
        return ToolResult(output="executed!")


async def test_fork_cannot_execute_ask_tools_without_approval(tmp_path: Path) -> None:
    """fork 技能不得绕过 permissions 层：ask 决策的工具在非交互执行中必须被拒绝。"""
    executed: list[str] = []
    parent_registry = ToolRegistry()
    parent_registry.register(RecordingBashTool(executed))

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )

    client = MockLLMClient(
        [
            [ToolCallComplete("t1", "Bash", {"command": "touch pwned.txt"})],
            [TextDelta("done"), StreamEnd("end_turn", input_tokens=1, output_tokens=1)],
        ]
    )
    agent = Agent(
        client=client,
        registry=parent_registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
        permission_checker=checker,
    )
    skill = SkillDef(
        name="risky",
        description="fork skill that shells out",
        prompt_body="run the command",
        mode="fork",
        allowed_tools=["Bash"],
        context="none",
    )

    executor = SkillExecutor(agent=agent, client=client, protocol="anthropic")
    result = await executor.execute_fork(skill, "")

    assert executed == [], f"fork 绕过权限门执行了工具: {executed}"
    assert isinstance(result, str)
