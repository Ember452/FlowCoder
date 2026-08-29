"""LLM 流式事件聚合。

StreamCollector 消费 client.stream 的底层 StreamEvent，
一边 yield 前端事件（StreamText/ThinkingText/ToolUseEvent），
一边拼出完整 LLMResponse 供后续写历史与用量统计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from flowcoder.agent.events import (
    AgentEvent,
    StreamText,
    ThinkingText,
    ToolUseEvent,
)
from flowcoder.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)


@dataclass
class ThinkingBlock:
    thinking: str  # 推理块内容
    signature: str  # 推理块签名


@dataclass
class LLMResponse:
    """大模型调用返回结果。"""

    text: str = ""
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class StreamCollector:
    """流式事件聚合器：消费底层 StreamEvent，双向产出。

    一边把面向前端的事件（StreamText / ThinkingText / ToolUseEvent）yield 出去
    供 TUI/Daemon/GUI 实时展示；一边把面向内部的状态（文本、思考块、工具调用、
    用量）累积到 ``self.response``，供 Agent 主循环在流结束后写历史与统计用量。
    """

    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(
        self,
        stream: AsyncIterator[StreamEvent],
    ) -> AsyncIterator[AgentEvent]:
        """消费 LLM 流式事件。

        Args:
            stream: LLM 流式事件迭代器。

        Yields:
            AgentEvent: 聚合后的 LLM 响应事件。
        """
        async for event in stream:
            if isinstance(event, TextDelta):
                # 正文增量：累积到 response.text 并转发给前端实时显示
                self.response.text += event.text
                yield StreamText(text=event.text)
            elif isinstance(event, ThinkingDelta):
                # 推理增量：只转发给前端，不累积（完整块在 Complete 时入库）
                yield ThinkingText(text=event.text)
            elif isinstance(event, ThinkingComplete):
                # 推理块结束：入库保存思考内容 + 签名（供下一轮 API 回传签名）
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking, signature=event.signature)
                )
            elif isinstance(event, ToolCallStart):
                # 工具调用开始：前端展示由 Delta/Complete 负责，这里忽略
                pass
            elif isinstance(event, ToolCallDelta):
                # 工具参数增量：参数最终在 Complete 时整体入库，这里忽略
                pass
            elif isinstance(event, ToolCallComplete):
                # 工具调用完成：入库并产出 ToolUseEvent（前端据此展示一次完整调用）
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamEnd):
                # 流结束：记录停止原因与用量，供用量统计与压缩阈值判断使用
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


__all__ = [
    "LLMResponse",
    "StreamCollector",
    "ThinkingBlock",
]
