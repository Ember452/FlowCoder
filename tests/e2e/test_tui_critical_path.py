"""e2e：TUI 关键路径（选 provider → 发消息 → 流式响应 → 收敛）。

不依赖真实 LLM：create_client 打桩为脚本化 client，TUI 用 Textual
run_test 驱动真实应用装配与消息回路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from flowcoder.client import LLMClient
from flowcoder.config import AppConfig, ProviderConfig
from flowcoder.conversation import ConversationManager
from flowcoder.tools.base import StreamEnd, TextDelta


class _FakeLLM(LLMClient):
    """单轮纯文本响应的 fake client。"""

    async def stream(
        self, conversation: ConversationManager, system: str = "", tools=None
    ) -> AsyncIterator[Any]:
        yield TextDelta(text="TUI e2e 回复：一切正常")
        yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=5)


@pytest.mark.asyncio
async def test_tui_send_message_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("flowcoder.app.create_client", lambda provider: _FakeLLM())

    from flowcoder.app import FlowCoderApp

    provider = ProviderConfig(
        name="fake",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/v1",
        model="fake-model",
    )
    config = AppConfig(providers=[provider])
    app = FlowCoderApp(providers=[provider], config=config)

    async with app.run_test(size=(100, 30)) as pilot:
        # on_mount 自动选择唯一 provider 并装配 agent
        assert app.agent is not None, "单 provider 应自动装配 Agent"

        # 关键路径：输入 → 提交 → 流式响应 → 收敛
        await app._send_message("你好")

        for _ in range(200):
            if not app._streaming:
                break
            await pilot.pause(0.05)
        assert not app._streaming, "流式应在有限时间内结束"

        # 对话状态：用户消息 + 助手最终回复，均真实入列
        messages = app.conversation.get_messages()
        assert any(m.role == "user" and "你好" in str(m.content) for m in messages)
        assert any("TUI e2e 回复" in str(m.content) for m in messages)


@pytest.mark.asyncio
async def test_tui_second_message_appends_to_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("flowcoder.app.create_client", lambda provider: _FakeLLM())

    from flowcoder.app import FlowCoderApp

    provider = ProviderConfig(
        name="fake",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/v1",
        model="fake-model",
    )
    app = FlowCoderApp(providers=[provider], config=AppConfig(providers=[provider]))

    async with app.run_test(size=(100, 30)) as pilot:
        await app._send_message("第一问")
        for _ in range(200):
            if not app._streaming:
                break
            await pilot.pause(0.05)
        await app._send_message("第二问")
        for _ in range(200):
            if not app._streaming:
                break
            await pilot.pause(0.05)

        messages = app.conversation.get_messages()
        user_turns = [m for m in messages if m.role == "user"]
        assert len(user_turns) >= 2
        # 最后一条可能是框架注入的 deferred 工具提醒，检查任意真实用户轮
        assert any("第二问" in str(m.content) for m in user_turns)
