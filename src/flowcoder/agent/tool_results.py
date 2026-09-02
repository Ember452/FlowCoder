"""工具结果块与前端事件构造。

把 ToolResult 转成写入 conversation 的 ToolResultBlock（可做内容落盘准备），
以及 yield 给 TUI/Daemon 的 ToolResultEvent；含 hook 拒绝时的错误结果。
"""

from __future__ import annotations

from pathlib import Path

from flowcoder.agent.events import ToolResultEvent
from flowcoder.context import prepare_tool_result_content
from flowcoder.conversation import ToolResultBlock
from flowcoder.tools.base import ToolCallComplete, ToolResult


def tool_result_block(
    tool_call: ToolCallComplete,
    result: ToolResult,
    session_dir: Path,
) -> ToolResultBlock:
    # 写回对话的块：超长内容经 prepare_tool_result_content 截断或落盘，保证上下文受控
    return ToolResultBlock(
        tool_use_id=tool_call.tool_id,
        content=prepare_tool_result_content(
            tool_call.tool_id,
            result.output,
            session_dir,
        ),
        is_error=result.is_error,
    )


def tool_result_event(
    tool_call: ToolCallComplete,
    result: ToolResult,
    elapsed: float,
) -> ToolResultEvent:
    # 实时事件：把执行结果与耗时推给前端展示，不含落盘后的完整内容
    return ToolResultEvent(
        tool_id=tool_call.tool_id,
        tool_name=tool_call.tool_name,
        output=result.output,
        is_error=result.is_error,
        elapsed=elapsed,
    )


def hook_rejected_result(reason: str) -> ToolResult:
    # pre_tool_use 拒绝时把原因做成错误结果，作为工具输出回喂给 LLM
    return ToolResult(
        output=f"Hook rejected: {reason}",
        is_error=True,
    )
