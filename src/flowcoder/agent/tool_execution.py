"""工具执行与批处理分区。

- partition_tool_calls: 只读安全工具可并行，写/执行类串行
- execute_direct_tool_call / execute_validated_tool: 实际调用工具
- StreamingExecutor 等执行辅助结构
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from flowcoder.tools import ToolRegistry
from flowcoder.tools.base import Tool, ToolCallComplete, ToolResult


@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    """把一轮里的多个工具调用切成 batch：只读安全工具可并行，写/执行类串行。

    分区规则：连续的 concurrency_safe 工具合并进同一个并行 batch；遇到非安全
    工具则另起一个串行 batch（concurrent=False）。这样既能让 LLM 一次发出的
    多个只读查询并行跑、又不至于让有副作用的工具互相干扰。
    """
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        # 安全 = 工具存在 + 标记为并发安全 + 当前未被禁用
        safe = tool is not None and tool.is_concurrency_safe and registry.is_enabled(tc.tool_name)

        if safe and batches and batches[-1].concurrent:
            # 当前是安全工具且上一个 batch 也是并行的 → 追加进去一起跑
            batches[-1].calls.append(tc)
        else:
            # 非安全工具或上一个 batch 是串行 → 另起一个新 batch
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


@dataclass
class _ToolExecResult:
    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool


@dataclass
class _AuthResult:
    """Tool authorization result. If approved is False, error holds the result."""

    approved: bool
    error: ToolResult | None = None
    is_unknown: bool = False


async def execute_validated_tool(tool: Tool, arguments: dict[str, Any]) -> ToolResult:
    # 用 pydantic 模型校验参数后再执行；校验失败和执行异常都转成 ToolResult 而非抛出
    try:
        params = tool.params_model.model_validate(arguments)
        return await tool.execute(params)
    except ValidationError as e:
        return ToolResult(output=f"Parameter validation error: {e}", is_error=True)
    except Exception as e:
        return ToolResult(output=f"Tool execution error: {e}", is_error=True)


async def execute_direct_tool_call(
    registry: ToolRegistry,
    tool_call: ToolCallComplete,
) -> _ToolExecResult:
    # 直接执行路径（无权限/Hook 拦截）：调用方须先完成授权预检。
    # 三种早退：工具不存在（is_unknown=True 计数，连续 3 次终止 Agent）、
    # 工具被禁用、正常执行。耗时用 monotonic 统一计量。
    start = time.monotonic()
    tool = registry.get(tool_call.tool_name)

    if tool is None:
        return _ToolExecResult(
            tool_id=tool_call.tool_id,
            tool_name=tool_call.tool_name,
            result=ToolResult(
                output=f"Error: unknown tool '{tool_call.tool_name}'",
                is_error=True,
            ),
            elapsed=time.monotonic() - start,
            is_unknown=True,
        )

    if not registry.is_enabled(tool_call.tool_name):
        return _ToolExecResult(
            tool_id=tool_call.tool_id,
            tool_name=tool_call.tool_name,
            result=ToolResult(
                output=f"Error: tool '{tool_call.tool_name}' is disabled",
                is_error=True,
            ),
            elapsed=time.monotonic() - start,
            is_unknown=False,
        )

    result = await execute_validated_tool(tool, tool_call.arguments)
    return _ToolExecResult(
        tool_id=tool_call.tool_id,
        tool_name=tool_call.tool_name,
        result=result,
        elapsed=time.monotonic() - start,
        is_unknown=False,
    )


class StreamingExecutor:
    """并行工具执行器：按提交顺序提交任务，按提交顺序回收结果。

    用自增序号记录提交顺序，回收时按序号排序，保证返回结果与提交顺序一致。
    gather 用 return_exceptions=True：单个工具抛异常不会让整批失败，
    而是降级成一条错误 ToolResult 返回。
    """

    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0

    def submit(
        self,
        coro: Any,
    ) -> None:
        # 创建后台任务并记录提交序号，便于回收时还原顺序
        task = asyncio.create_task(coro)
        self._tasks.append((self._order, task))
        self._order += 1

    async def collect_results(self) -> list[_ToolExecResult]:
        if not self._tasks:
            return []
        # 按提交序号排序后再 gather，结果顺序与提交一致
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, Exception):
                # 异常降级：转成错误 ToolResult，不让单个失败拖垮整批
                out.append(
                    _ToolExecResult(
                        tool_id="",
                        tool_name="",
                        result=ToolResult(
                            output=f"Tool execution error: {r}",
                            is_error=True,
                        ),
                        elapsed=0.0,
                        is_unknown=False,
                    )
                )
            else:
                out.append(r)
        return out
