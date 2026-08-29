"""SerializedSendGate 测试：_send_message 触发源串行化（P0-8 回归）。"""

from __future__ import annotations

import asyncio

import pytest

from flowcoder.app import SerializedSendGate


@pytest.mark.asyncio
async def test_gate_serializes_concurrent_submissions():
    """同一 tick 的两个 submit 必须顺序执行，不并发。"""
    active = 0
    max_active = 0
    ran: list[str] = []

    async def runner(text: str) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        ran.append(text)
        active -= 1

    gate = SerializedSendGate(runner)
    # 模拟两个触发源在同一事件循环 tick 内 submit（旧实现下会并发执行）
    gate.submit("first")
    gate.submit("second")
    pump = gate.current_task
    assert pump is not None
    await pump

    assert ran == ["first", "second"]
    assert max_active == 1, "两个 _send_message 并发执行了"


@pytest.mark.asyncio
async def test_gate_cancel_drops_queued_requests():
    started: list[str] = []

    async def runner(text: str) -> None:
        started.append(text)
        await asyncio.sleep(0.05)

    gate = SerializedSendGate(runner)
    gate.submit("current")
    await asyncio.sleep(0.005)  # 让泵进入 runner
    gate.submit("queued")
    gate.cancel()

    with pytest.raises(asyncio.CancelledError):
        await gate.current_task

    await asyncio.sleep(0.06)
    assert "queued" not in started, "取消后排队的请求不应补发"


@pytest.mark.asyncio
async def test_gate_pump_restarts_after_completion():
    ran: list[str] = []

    async def runner(text: str) -> None:
        ran.append(text)

    gate = SerializedSendGate(runner)
    gate.submit("a")
    await gate.current_task
    gate.submit("b")
    await gate.current_task

    assert ran == ["a", "b"]
