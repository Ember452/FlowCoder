"""LLM 调用韧性层：错误分类重试、指数退避、进程内令牌桶限流。

设计取向（对照 PROMPTS.md P3，借鉴 flow-agent resilience 思想、按本项目
异步生成器风格重写）：

- 位置：并入 client 包而非顶层 resilience.py——重试分类依赖 client.errors
  的 LLMError 体系，且 client 是最底层（不依赖任何上层），顶层模块反而会
  形成向上的反向依赖。
- 流式重试边界：错误发生在**任何事件产出之前**才能安全重试（从头重放）；
  流中途失败无法重放，原样向消费方抛出。这是流式语义下唯一正确的取舍。
- 可重试错误：RateLimitError（尊重 retry-after）、NetworkError、
  ServerError（5xx）、LLMTimeoutError（单次请求超时）。
  AuthenticationError / 4xx 不可重试——立即失败，不烧重试预算。
- 令牌桶：进程内异步实现，按 RPM 平滑请求速率；多客户端可共享同一桶。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, AsyncIterator

from flowcoder.client.core import LLMClient
from flowcoder.client.errors import (
    LLMError,
    LLMTimeoutError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from flowcoder.conversation import ConversationManager

logger = logging.getLogger(__name__)

RETRYABLE_ERRORS = (RateLimitError, NetworkError, ServerError, LLMTimeoutError)

DEFAULT_MAX_RETRIES = 2
DEFAULT_BASE_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 8.0
DEFAULT_JITTER_S = 0.25


def is_retryable(error: BaseException) -> bool:
    """错误分类：是否值得重试。鉴权失败与 4xx 立即失败。"""
    return isinstance(error, RETRYABLE_ERRORS)


def backoff_delay(
    attempt: int,
    *,
    base_s: float = DEFAULT_BASE_BACKOFF_S,
    max_s: float = DEFAULT_MAX_BACKOFF_S,
    jitter_s: float = DEFAULT_JITTER_S,
    retry_after: float | None = None,
) -> float:
    """指数退避 + 抖动。attempt 从 0 计（第 1 次重试前）。

    retry-after 优先级：取 provider 建议值与本地计算的较大者——
    provider 的限流窗口是权威，但本地指数下限防止 retry-after=0 的空转。
    """
    exponential = min(max_s, base_s * (2**attempt))
    delay = exponential + random.uniform(0, jitter_s)  # noqa: S311
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(delay, max_s + jitter_s)


class TokenBucket:
    """进程内异步令牌桶：平滑请求速率（RPM），支持并发等待。

    惰性补充模型：按流逝时间补充令牌（容量封顶），acquire 时不足则
    等待差额时间。全部用 asyncio 原语，不阻塞事件循环。
    """

    def __init__(self, rate_per_min: float, capacity: float | None = None) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min 必须为正数")
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_min / 60.0)
        if self.capacity <= 0:
            raise ValueError("capacity 必须为正数")
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        self._tokens = min(
            self.capacity, self._tokens + (now - self._last_refill) * self.rate_per_sec
        )
        self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> float:
        """非阻塞尝试；成功返回 0，失败返回需要等待的秒数。"""
        if tokens > self.capacity:
            raise ValueError("单次请求令牌数不能超过桶容量")
        now = time.monotonic()
        self._refill(now)
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        return (tokens - self._tokens) / self.rate_per_sec

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                wait_s = self.try_acquire(tokens)
            if wait_s <= 0:
                return
            await asyncio.sleep(wait_s)


class ResilientClient(LLMClient):
    """LLMClient 装饰器：限流 + 重试 + 单请求超时。

    对消费方完全透明（stream 签名/事件流不变），在工厂层包裹真实 client
    即可让 Agent 循环零改动获得韧性。
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff_s: float = DEFAULT_BASE_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        jitter_s: float = DEFAULT_JITTER_S,
        request_timeout_s: float | None = None,
        bucket: TokenBucket | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数")
        self._inner = inner
        self._max_retries = max_retries
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s
        self._jitter_s = jitter_s
        self._request_timeout_s = request_timeout_s
        self._bucket = bucket

    def set_max_output_tokens(self, tokens: int) -> None:
        self._inner.set_max_output_tokens(tokens)

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        last_error: BaseException | None = None
        for attempt in range(self._max_retries + 1):
            if self._bucket is not None:
                await self._bucket.acquire()
            # yielded：是否已有事件交付给消费方。流中途失败无法重放，
            # 只有"零交付"的失败才允许整体重试
            yielded = False
            try:
                async for event in self._stream_attempt(conversation, system, tools):
                    yielded = True
                    yield event
                return
            except asyncio.CancelledError:
                raise  # 取消语义放行（AGENTS.md 第五节）
            except LLMError as e:
                last_error = e
                if yielded or not is_retryable(e) or attempt >= self._max_retries:
                    raise
                retry_after = getattr(e, "retry_after", None)
                delay = backoff_delay(
                    attempt,
                    base_s=self._base_backoff_s,
                    max_s=self._max_backoff_s,
                    jitter_s=self._jitter_s,
                    retry_after=retry_after,
                )
                logger.warning(
                    "LLM 调用失败（第 %d/%d 次重试前）：%.1fs 后重试 — %s",
                    attempt + 1,
                    self._max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

        assert last_error is not None  # pragma: no cover - 循环结构保证
        raise last_error

    async def _stream_attempt(
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[Any]:
        """单次尝试（重试判定在外层：见 stream 的 yielded 标记）。"""
        stream = self._inner.stream(conversation, system=system, tools=tools)
        if self._request_timeout_s is not None:
            # 超时覆盖整个流式过程：无事件产出超过阈值视为超时（事件持续
            # 产出则不断续期），兜住"连接成功但永远不出首事件"的挂死
            stream = _timeout_guard(stream, self._request_timeout_s)
        try:
            async for event in stream:
                yield event
        except asyncio.CancelledError:
            raise
        finally:
            await stream.aclose()


async def _timeout_guard(stream: AsyncIterator[Any], timeout_s: float) -> AsyncIterator[Any]:
    """无事件产出超过 timeout_s 视为超时；事件持续产出则不断续期。"""
    it = stream.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(anext(it), timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise LLMTimeoutError(f"LLM 流式无产出超过 {timeout_s}s") from e
        except StopAsyncIteration:
            return
        yield event
