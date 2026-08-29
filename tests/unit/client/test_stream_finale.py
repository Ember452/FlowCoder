"""流收尾保底测试：无论何种退出路径，恰好一个 StreamEnd。

对应 CODE_QUALITY_AUDIT.md P0-5/P0-6：
- Chat Completions 无 usage chunk（vLLM/Ollama）或断流时原先不发 StreamEnd
- Responses API 的 response.failed/incomplete/error 被白名单静默丢弃
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from flowcoder.client import OpenAIClient, OpenAICompatClient
from flowcoder.conversation import ConversationManager
from flowcoder.providers._stream_common import with_guaranteed_stream_end
from flowcoder.tools.base import StreamEnd, StreamEvent, TextDelta


class _AsyncStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


async def _collect(stream):
    result = []
    async for event in stream:
        result.append(event)
    return result


def _ends(events: list[StreamEvent]) -> list[StreamEnd]:
    return [e for e in events if isinstance(e, StreamEnd)]


def _responses_client(stream_events) -> OpenAIClient:
    client = OpenAIClient.__new__(OpenAIClient)
    client.model = "gpt-test"
    client.thinking = False
    client.max_output_tokens = 1024
    client._client = SimpleNamespace(
        responses=SimpleNamespace(
            create=SimpleNamespace(),  # 占位，真正返回在下方闭包里
        )
    )

    async def create(**_kwargs):
        return _AsyncStream(stream_events)

    client._client.responses.create = create
    return client


def _compat_client(stream_events) -> OpenAICompatClient:
    client = OpenAICompatClient.__new__(OpenAICompatClient)
    client.model = "compat-test"
    client.thinking = False
    client.max_output_tokens = 1024

    async def create(**_kwargs):
        return _AsyncStream(stream_events)

    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client


# ---------------------------------------------------------------------------
# Responses API（P0-6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responses_failed_event_yields_error_stream_end():
    events = await _collect(
        _responses_client(
            [
                SimpleNamespace(type="response.output_text.delta", delta="部分"),
                SimpleNamespace(
                    type="response.failed",
                    response=SimpleNamespace(usage=None),
                ),
            ]
        ).stream(ConversationManager())
    )
    ends = _ends(events)
    assert len(ends) == 1
    assert ends[0].stop_reason == "error"


@pytest.mark.asyncio
async def test_responses_incomplete_event_yields_max_tokens_stream_end():
    events = await _collect(
        _responses_client(
            [
                SimpleNamespace(type="response.output_text.delta", delta="截断"),
                SimpleNamespace(
                    type="response.incomplete",
                    response=SimpleNamespace(usage=None),
                ),
            ]
        ).stream(ConversationManager())
    )
    ends = _ends(events)
    assert len(ends) == 1
    assert ends[0].stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_responses_broken_stream_still_yields_single_stream_end():
    """断流：上游在 response.completed 前静默结束。"""
    events = await _collect(
        _responses_client([SimpleNamespace(type="response.output_text.delta", delta="断")]).stream(
            ConversationManager()
        )
    )
    assert len(_ends(events)) == 1


# ---------------------------------------------------------------------------
# Chat Completions（P0-5）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compat_without_usage_chunk_still_yields_single_stream_end():
    """vLLM/Ollama 等不发 usage chunk：finish_reason=stop 后直接结束。"""
    events = await _collect(
        _compat_client(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="hi", tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )
            ]
        ).stream(ConversationManager())
    )
    assert len(_ends(events)) == 1


@pytest.mark.asyncio
async def test_compat_with_usage_chunk_yields_exactly_one_stream_end():
    events = await _collect(
        _compat_client(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="hi", tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=10,
                        completion_tokens=5,
                        prompt_tokens_details=None,
                    ),
                ),
            ]
        ).stream(ConversationManager())
    )
    ends = _ends(events)
    assert len(ends) == 1
    assert ends[0].input_tokens == 10
    assert ends[0].output_tokens == 5


@pytest.mark.asyncio
async def test_compat_broken_stream_still_yields_single_stream_end():
    """断流：无 finish_reason 直接结束。"""
    events = await _collect(
        _compat_client(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="断", tool_calls=None),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                )
            ]
        ).stream(ConversationManager())
    )
    assert len(_ends(events)) == 1


# ---------------------------------------------------------------------------
# 包装器本身
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_suppresses_duplicate_stream_end():
    async def dup():
        yield TextDelta(text="x")
        yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
        yield StreamEnd(stop_reason="end_turn", input_tokens=2, output_tokens=2)

    events = await _collect(
        with_guaranteed_stream_end(dup(), lambda: StreamEnd(stop_reason="error"))
    )
    assert len(_ends(events)) == 1


@pytest.mark.asyncio
async def test_wrapper_passes_exceptions_through():
    async def broken():
        yield TextDelta(text="x")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _collect(with_guaranteed_stream_end(broken(), lambda: StreamEnd(stop_reason="error")))
