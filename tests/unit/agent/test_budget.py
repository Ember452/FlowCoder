"""四维预算闸测试：收敛而非硬杀（P3）。"""

from __future__ import annotations

import pytest

from flowcoder.agent import Agent, ErrorEvent, LoopComplete, StreamText
from flowcoder.agent.budget import Budget, BudgetState, converge_message
from flowcoder.client import LLMClient
from flowcoder.conversation import ConversationManager
from flowcoder.tools import create_default_registry
from flowcoder.tools.base import StreamEnd, TextDelta, ToolCallComplete


class ScriptedClient(LLMClient):
    """每轮产出工具调用 + 文本；检测到预算告警后转为纯文本收敛。"""

    def __init__(self) -> None:
        self.round = 0

    async def stream(self, conversation: ConversationManager, system: str = "", tools=None):
        self.round += 1
        yield TextDelta(text=f"round-{self.round}")
        warned = any("预算告警" in str(m.content) for m in conversation.get_messages())
        if not warned:
            yield ToolCallComplete(
                tool_id=f"call-{self.round}",
                tool_name="Bash",
                arguments={"command": "echo hi"},
            )
        yield StreamEnd(stop_reason="end_turn", input_tokens=100, output_tokens=10)


class RebelClient(LLMClient):
    """收敛轮仍然调用工具的无赖模型（验证强制收场）。"""

    async def stream(self, conversation: ConversationManager, system: str = "", tools=None):
        yield TextDelta(text="ignore-converge")
        yield ToolCallComplete(tool_id="call-x", tool_name="Bash", arguments={"command": "echo"})
        yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=1)


def _agent(client: LLMClient, budget: Budget | None = None, **kwargs: object) -> Agent:
    return Agent(
        client=client,
        registry=create_default_registry(),
        protocol="anthropic",
        work_dir=".",
        budget=budget,
        **kwargs,
    )


async def _run(agent: Agent) -> tuple[list, ConversationManager]:
    conversation = ConversationManager()
    conversation.add_user_message("task")
    events = [e async for e in agent.run(conversation)]
    return events, conversation


class TestBudgetValidation:
    def test_empty_budget_rejected(self) -> None:
        with pytest.raises(ValueError, match="至少"):
            Budget()

    def test_non_positive_limits_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_turns"):
            Budget(max_turns=0)
        with pytest.raises(ValueError, match="max_seconds"):
            Budget(max_seconds=-1)

    def test_cost_requires_pricing(self) -> None:
        with pytest.raises(ValueError, match="单价"):
            Budget(max_cost_usd=1.0)

    def test_cost_computation(self) -> None:
        b = Budget(max_cost_usd=1.0, input_price_per_1m=3.0, output_price_per_1m=15.0)
        assert b.cost_usd(1_000_000, 100_000) == pytest.approx(3.0 + 1.5)

    def test_breach_reasons(self) -> None:
        state = BudgetState(Budget(max_total_tokens=100, max_turns=2, max_seconds=999))
        assert (
            state.breach_reason(total_input_tokens=50, total_output_tokens=50, iteration=1) is None
        )
        assert "token" in state.breach_reason(
            total_input_tokens=90, total_output_tokens=20, iteration=1
        )
        assert "轮次" in state.breach_reason(
            total_input_tokens=0, total_output_tokens=0, iteration=3
        )

    def test_converge_message_contains_reason(self) -> None:
        assert "token 超限" in converge_message("token 超限（1 > 0）")


class TestBudgetConvergence:
    async def test_token_budget_triggers_converge_not_hard_kill(self) -> None:
        # 每轮 110 tokens，预算 300：第 4 轮开头超限 → 注入收敛请求；
        # 工具 schema 撤下 → 模型纯文本收敛，LoopComplete 正常产出
        client = ScriptedClient()
        agent = _agent(client, Budget(max_total_tokens=300))
        events, conversation = await _run(agent)

        assert any(isinstance(e, StreamText) for e in events)
        assert any(isinstance(e, LoopComplete) for e in events)  # 正常收敛，非硬杀
        assert not any(isinstance(e, ErrorEvent) for e in events)
        assert agent._budget_converging
        assert any("预算告警" in str(m) for m in conversation.get_messages())
        # 收敛轮无工具调用可执行（工具 schema 已撤下，脚本工具调用被忽略）
        assert client.round == 4

    async def test_rebel_model_force_stopped(self) -> None:
        # 收敛轮仍调用工具 → 强制结束（ErrorEvent + LoopComplete）
        client = RebelClient()
        agent = _agent(client, Budget(max_total_tokens=1))
        events, _ = await _run(agent)
        assert any(isinstance(e, ErrorEvent) for e in events)
        assert any(isinstance(e, LoopComplete) for e in events)

    async def test_turn_budget(self) -> None:
        client = ScriptedClient()
        agent = _agent(client, Budget(max_turns=2))
        events, conversation = await _run(agent)
        assert any(isinstance(e, LoopComplete) for e in events)
        assert any("轮次超限" in str(m) for m in conversation.get_messages())

    async def test_no_budget_unchanged(self) -> None:
        # 默认无预算：跑满 max_iterations 后以 ErrorEvent 截断（原有行为）
        client = ScriptedClient()
        agent = _agent(client, max_iterations=3)
        events, _ = await _run(agent)
        assert any(isinstance(e, ErrorEvent) and "maximum iterations" in e.message for e in events)
        assert client.round == 3
