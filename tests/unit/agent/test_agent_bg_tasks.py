"""Agent 后台任务管理测试（P0-9 回归：fire-and-forget 必须有引用与异常日志）。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from flowcoder.agent import Agent
from flowcoder.client import LLMClient
from flowcoder.tools import ToolRegistry


class _NoopClient(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        yield None  # pragma: no cover - 不会被消费


def _make_agent() -> Agent:
    return Agent(client=_NoopClient(), registry=ToolRegistry(), protocol="anthropic")


@pytest.mark.asyncio
async def test_bg_task_keeps_reference_and_cleans_up(caplog):
    agent = _make_agent()

    async def work() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    task = agent._spawn_bg(work())
    assert len(agent._bg_tasks) == 1, "后台任务必须被强引用，否则可能被 GC 中途回收"

    await task
    assert len(agent._bg_tasks) == 0, "完成的任务应从引用集移除"


@pytest.mark.asyncio
async def test_bg_task_exception_is_logged(caplog: pytest.LogCaptureFixture):
    agent = _make_agent()

    async def boom() -> None:
        raise RuntimeError("bg boom")

    task = agent._spawn_bg(boom())
    with caplog.at_level(logging.ERROR):
        await asyncio.gather(task, return_exceptions=True)

    assert any("bg boom" in record.message for record in caplog.records), (
        "后台任务异常必须有日志，不能静默丢失"
    )


@pytest.mark.asyncio
async def test_bg_task_cancellation_is_not_logged_as_error(caplog):
    agent = _make_agent()

    async def slow() -> None:
        await asyncio.sleep(10)

    task = asyncio.ensure_future(slow())
    agent._bg_tasks.add(task)
    task.cancel()
    with caplog.at_level(logging.ERROR):
        await asyncio.gather(task, return_exceptions=True)
    agent._on_bg_task_done(task)

    assert not any("background task failed" in r.message for r in caplog.records)
    assert len(agent._bg_tasks) == 0
