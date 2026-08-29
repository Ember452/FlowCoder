"""韧性层测试：错误分类、退避序列、令牌桶并发、ResilientClient 重试。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

import pytest

from flowcoder.client.core import LLMClient
from flowcoder.client.errors import (
    AuthenticationError,
    LLMError,
    LLMTimeoutError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from flowcoder.client.resilience import (
    ResilientClient,
    TokenBucket,
    backoff_delay,
    is_retryable,
)
from flowcoder.conversation import ConversationManager
from flowcoder.tools.base import StreamEnd, TextDelta

_OK = [TextDelta(text="ok"), StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)]


class FakeStreamClient(LLMClient):
    """按脚本抛错/产出的 fake client。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def stream(
        self, conversation: ConversationManager, system: str = "", tools=None
    ) -> AsyncIterator[Any]:
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, BaseException):
            raise item
        for event in item:
            yield event


async def _collect(client: LLMClient) -> list[Any]:
    return [e async for e in client.stream(ConversationManager())]


class TestClassification:
    def test_retryable_errors(self) -> None:
        assert is_retryable(RateLimitError("429", retry_after=1.0))
        assert is_retryable(NetworkError("conn"))
        assert is_retryable(ServerError("500", status_code=500))
        assert is_retryable(LLMTimeoutError("timeout"))

    def test_non_retryable_errors(self) -> None:
        assert not is_retryable(AuthenticationError("bad key"))
        assert not is_retryable(LLMError("API error (400): bad request"))
        assert not is_retryable(ValueError("unrelated"))

    def test_5xx_mapped_to_server_error(self) -> None:
        from flowcoder.client.error_mapping import provider_error_mapper

        class FakeSdk:
            class AuthenticationError(Exception): ...

            class RateLimitError(Exception): ...

            class APIConnectionError(Exception): ...

            class APIStatusError(Exception): ...

        class ApiErr(FakeSdk.APIStatusError):
            status_code = 503

        mapper = provider_error_mapper(FakeSdk)
        mapped = mapper.to_llm_error(ApiErr("down"))
        assert isinstance(mapped, ServerError)
        assert mapped.status_code == 503


class TestBackoff:
    def test_exponential_sequence(self) -> None:
        delays = [backoff_delay(i, jitter_s=0) for i in range(5)]
        assert delays == [0.5, 1.0, 2.0, 4.0, 8.0]  # 8.0 封顶

    def test_jitter_adds_uncertainty(self) -> None:
        d = backoff_delay(0, jitter_s=0.25)
        assert 0.5 <= d <= 0.75

    def test_retry_after_wins_when_larger(self) -> None:
        assert backoff_delay(0, jitter_s=0, retry_after=30.0) == 8.0  # 封顶
        assert backoff_delay(0, base_s=0.1, jitter_s=0, retry_after=1.5) == 1.5

    def test_total_cap(self) -> None:
        d = backoff_delay(10, base_s=0.5, max_s=8.0, jitter_s=0.25)
        assert d <= 8.0 + 0.25


class TestTokenBucket:
    def test_rejects_invalid_args(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(0)
        with pytest.raises(ValueError):
            TokenBucket(60, capacity=0)

    async def test_burst_up_to_capacity_then_waits(self) -> None:
        bucket = TokenBucket(rate_per_min=60, capacity=2)  # 1 req/s，容量 2
        assert bucket.try_acquire() == 0.0
        assert bucket.try_acquire() == 0.0
        wait = bucket.try_acquire()
        assert wait > 0.9  # 需要等约 1s 补充

    async def test_concurrent_acquire_throttles(self) -> None:
        # 6 个并发请求，600/min（10/s）、容量 1：5 个等待间隔 × ~0.1s
        bucket = TokenBucket(rate_per_min=600, capacity=1)
        start = time.monotonic()
        await asyncio.gather(*(bucket.acquire() for _ in range(6)))
        elapsed = time.monotonic() - start
        assert elapsed >= 0.4

    async def test_token_amount_larger_than_capacity_rejected(self) -> None:
        bucket = TokenBucket(rate_per_min=60, capacity=2)
        with pytest.raises(ValueError):
            bucket.try_acquire(3)


class TestResilientClient:
    async def test_retry_on_rate_limit_then_success(self) -> None:
        inner = FakeStreamClient([RateLimitError("429", retry_after=0.0), _OK])
        client = ResilientClient(inner, base_backoff_s=0.01, max_backoff_s=0.02)
        events = await _collect(client)
        assert events[0].text == "ok"
        assert inner.calls == 2

    async def test_retry_exhaustion_raises_last_error(self) -> None:
        inner = FakeStreamClient([NetworkError("down")] * 5)
        client = ResilientClient(inner, max_retries=2, base_backoff_s=0.01)
        with pytest.raises(NetworkError):
            await _collect(client)
        assert inner.calls == 3  # 1 次原始 + 2 次重试

    async def test_no_retry_on_auth_error(self) -> None:
        inner = FakeStreamClient([AuthenticationError("bad key")])
        client = ResilientClient(inner, max_retries=3, base_backoff_s=0.01)
        with pytest.raises(AuthenticationError):
            await _collect(client)
        assert inner.calls == 1

    async def test_no_retry_after_first_event_yielded(self) -> None:
        # 流中途失败：无法重放，原样抛出（流式语义下的正确取舍）
        class MidStreamFail(LLMClient):
            def __init__(self) -> None:
                self.calls = 0

            async def stream(self, conversation, system="", tools=None):
                self.calls += 1
                yield TextDelta(text="partial")
                raise NetworkError("mid-stream drop")

        failing = MidStreamFail()
        client = ResilientClient(failing, max_retries=3, base_backoff_s=0.01)
        with pytest.raises(NetworkError):
            await _collect(client)
        assert failing.calls == 1

    async def test_request_timeout_wrapped(self) -> None:
        class SlowClient(LLMClient):
            async def stream(self, conversation, system="", tools=None):
                await asyncio.sleep(1.0)
                yield TextDelta(text="late")

        client = ResilientClient(SlowClient(), max_retries=0, request_timeout_s=0.05)
        with pytest.raises(LLMTimeoutError):
            await _collect(client)

    async def test_timeout_retries(self) -> None:
        calls = 0

        class SlowThenFast(LLMClient):
            async def stream(self, conversation, system="", tools=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    await asyncio.sleep(1.0)
                yield TextDelta(text="ok")

        client = ResilientClient(
            SlowThenFast(), max_retries=1, base_backoff_s=0.01, request_timeout_s=0.05
        )
        events = await _collect(client)
        assert events[0].text == "ok"
        assert calls == 2

    async def test_rate_limit_bucket_throttles(self) -> None:
        inner = FakeStreamClient([_OK])
        bucket = TokenBucket(rate_per_min=600, capacity=1)  # 10 req/s
        client = ResilientClient(inner, bucket=bucket)
        start = time.monotonic()
        for _ in range(3):
            await _collect(client)
        assert time.monotonic() - start >= 0.15  # 两次间隔 × ~0.1s

    async def test_cancellation_propagates(self) -> None:
        class SlowClient(LLMClient):
            async def stream(self, conversation, system="", tools=None):
                await asyncio.sleep(10)
                yield TextDelta(text="never")

        client = ResilientClient(SlowClient(), max_retries=5, base_backoff_s=0.01)
        agen = client.stream(ConversationManager())
        task = asyncio.create_task(anext(agen))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await agen.aclose()

    async def test_invalid_max_retries(self) -> None:
        with pytest.raises(ValueError):
            ResilientClient(FakeStreamClient([]), max_retries=-1)
