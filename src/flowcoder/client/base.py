"""LLMClient 统一抽象（P5 重构拆分，行为零变化）。

协议与实现混杂的拆分：抽象接口独立成文件（AGENTS.md 拆分信号 5——
实现有多个：Anthropic / OpenAI Responses / OpenAI Compat），三协议实现
留在 core.py，工厂在 factory.py。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from flowcoder.conversation import ConversationManager
from flowcoder.tools.base import TextDelta, StreamEvent


class LLMClient(ABC):
    """LLM 客户端统一抽象：屏蔽 Anthropic / OpenAI / OpenAI 兼容三家差异。

    对外只暴露 ``stream()``——把对话 + system prompt + tools 转成统一的
    ``StreamEvent`` 流（TextDelta / ThinkingDelta / ToolCall* / StreamEnd），
    Agent 侧无需关心底层用的是哪家协议。子类负责把各自 SDK 的事件
    翻译成这套统一事件流。
    """

    #: 采样温度（provider 配置透传；None=provider 默认）。类级默认兜底：
    #: 部分测试绕过 __init__ 构造实例
    temperature: float | None = None

    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta("")

    def set_max_output_tokens(self, tokens: int) -> None:
        pass
