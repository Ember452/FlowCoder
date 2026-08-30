"""客户端工厂（P5 重构拆分）：协议选择 + 韧性层统一包裹。

消费方经 `flowcoder.client.create_client` 引用（包导出不变），
Agent/TUI/eval 零改动获得重试/限流/超时韧性（P3）。
"""

from __future__ import annotations

from flowcoder.client.base import LLMClient
from flowcoder.client.core import (
    AnthropicClient,
    OpenAIClient,
    OpenAICompatClient,
)
from flowcoder.client.context_window import (
    resolve_context_window as _resolve_context_window,
)
from flowcoder.config import ProviderConfig


def create_client(config: ProviderConfig) -> LLMClient:
    # 协议工厂：根据 config.protocol 选用对应客户端，对外返回统一的 LLMClient；
    # 外层统一包 ResilientClient（重试/限流/超时，P3），对消费方透明
    if config.protocol == "anthropic":
        inner: LLMClient = AnthropicClient(config)
    elif config.protocol == "openai":
        inner = OpenAIClient(config)
    elif config.protocol == "openai-compat":
        inner = OpenAICompatClient(config)
    else:
        raise ValueError(f"Unknown protocol: {config.protocol}")

    from flowcoder.client.resilience import ResilientClient, TokenBucket

    bucket = TokenBucket(config.rate_limit_rpm) if config.rate_limit_rpm else None
    return ResilientClient(
        inner,
        max_retries=config.max_retries,
        request_timeout_s=config.request_timeout_s,
        bucket=bucket,
    )


async def resolve_context_window(config: ProviderConfig) -> None:
    """context window 解析的第 2 层：对于 anthropic 协议的 provider，
    从 {base_url}/v1/models/{model} 自动拉取一次模型的 max_input_tokens，
    并通过 set_fetched_context_window 缓存到 ``config`` 上，这样后续
    config.get_context_window() 调用就能直接使用、无需再次访问网络。

    完全尽力而为，绝不抛出异常：非 anthropic provider、客户端构造失败
    （例如缺少 API key）、拉取失败或超时，都会让缓存保持不变，从而让
    get_context_window() 降级到内置映射表 / 默认值。在启动时调用是安全的——
    阻塞时间不会超过拉取自身的超时，也不会导致崩溃。
    """
    await _resolve_context_window(config, create_client)
