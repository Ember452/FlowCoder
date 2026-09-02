"""Anthropic 流式载荷 → 内部 StreamEvent。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from flowcoder.tools.base import (
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """把流式累加的参数字符串解析成 dict；空串或非法 JSON 兜底为空 dict。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class AnthropicStreamState:
    """解析 Anthropic Messages API 的流式 content_block。

    Anthropic 的流式以"块生命周期"组织：``content_block_start`` 给出块身份
    （thinking 或 tool_use），随后若干 ``content_block_delta`` 增量推送块内容，
    最后 ``content_block_stop`` 收尾。本状态机在 start 时记录当前块类型，
    在 delta 时按类型累加，在 stop 时产出完整的 ThinkingComplete / ToolCallComplete。
    同一时刻只跟踪一个块，故用扁平字段即可。
    """

    tool_name: str = ""
    tool_id: str = ""
    arguments_json: str = ""
    in_thinking: bool = False
    thinking: str = ""
    thinking_signature: str = ""

    def start_block(self, block: Any) -> ToolCallStart | None:
        # content_block_start：根据块类型切换当前跟踪状态
        block_type = getattr(block, "type", "")
        if block_type == "thinking":
            # 进入思考块：重置思考缓冲，等待 thinking_delta 填充
            self.in_thinking = True
            self.thinking = ""
            self.thinking_signature = ""
            return None
        if block_type == "tool_use":
            # 进入工具调用块：块头就带 name 和 id，立即发出 ToolCallStart
            self.tool_name = getattr(block, "name", "") or ""
            self.tool_id = getattr(block, "id", "") or ""
            self.arguments_json = ""
            return ToolCallStart(tool_name=self.tool_name, tool_id=self.tool_id)
        return None

    def add_delta(self, delta: Any) -> list[ThinkingDelta | ToolCallDelta]:
        # content_block_delta：按 delta 类型分发到当前块的缓冲
        delta_type = getattr(delta, "type", "")
        if delta_type == "thinking_delta":
            # 思考正文增量：累加并转发
            text = getattr(delta, "thinking", "") or ""
            self.thinking += text
            return [ThinkingDelta(text=text)]
        if delta_type == "signature_delta":
            # 思考签名：API 用于校验 extended thinking，stop 时随 ThinkingComplete 带回
            self.thinking_signature = getattr(delta, "signature", "") or ""
            return []
        if delta_type == "input_json_delta":
            # 工具参数 JSON 增量：累加到 arguments_json，stop 时整体解析
            text = getattr(delta, "partial_json", "") or ""
            self.arguments_json += text
            return [ToolCallDelta(text=text)]
        return []

    def stop_block(self) -> list[ThinkingComplete | ToolCallComplete]:
        # content_block_stop：把当前块收尾为完整事件并清空状态
        events: list[ThinkingComplete | ToolCallComplete] = []
        if self.in_thinking:
            events.append(
                ThinkingComplete(
                    thinking=self.thinking,
                    signature=self.thinking_signature,
                )
            )
            self.in_thinking = False
            self.thinking = ""
            self.thinking_signature = ""
        if self.tool_name:
            events.append(
                ToolCallComplete(
                    tool_id=self.tool_id,
                    tool_name=self.tool_name,
                    arguments=parse_tool_arguments(self.arguments_json),
                )
            )
            self.tool_name = ""
            self.tool_id = ""
            self.arguments_json = ""
        return events
