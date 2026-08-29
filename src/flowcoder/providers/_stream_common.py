"""协议无关的流收尾保证。

StreamEnd 是上层（StreamCollector / Agent 主循环）判断一轮 LLM 响应结束的
唯一信号：用量统计、历史写入都依赖它。三种协议的流循环各自实现收尾，
异常路径（无 usage chunk、response.failed 被忽略、断流）容易漏发，
导致上层拿不到终止事件。本包装器把"恰好一个 StreamEnd"变成协议无关的
结构性保证，各 client 的流循环只需包一层。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

from flowcoder.tools.base import StreamEnd, StreamEvent

log = logging.getLogger(__name__)


async def with_guaranteed_stream_end(
    stream: AsyncIterator[StreamEvent],
    fallback: Callable[[], StreamEnd],
) -> AsyncIterator[StreamEvent]:
    """保证流以恰好一个 StreamEnd 结束。

    - 上游正常发出 StreamEnd：原样透传，多余的重复 StreamEnd 被抑制；
    - 上游静默结束（没发过 StreamEnd）：补发 ``fallback()`` 并记警告日志；
    - 上游抛异常：不补发，异常原样传播——错误路径由 client 层错误体系处理。
    """
    saw_end = False
    async for event in stream:
        if isinstance(event, StreamEnd):
            if saw_end:
                log.warning("duplicate StreamEnd suppressed")
                continue
            saw_end = True
        yield event
    if not saw_end:
        log.warning("stream ended without StreamEnd; synthesizing fallback end")
        yield fallback()
