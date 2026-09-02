"""Provider 异常到统一 LLMError 的映射。"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from flowcoder.client.errors import (
    AuthenticationError,
    LLMError,
    NetworkError,
    ServerError,
    rate_limit_error,
)


ExceptionTypes = tuple[type[BaseException], ...]


def _exception_tuple(value: type[BaseException] | ExceptionTypes) -> ExceptionTypes:
    if isinstance(value, tuple):
        return value
    return (value,)


@dataclass(frozen=True)
class ProviderErrorMapper:
    authentication_errors: ExceptionTypes
    rate_limit_errors: ExceptionTypes
    connection_errors: ExceptionTypes
    status_errors: ExceptionTypes

    @property
    def handled_errors(self) -> ExceptionTypes:
        return (
            *self.authentication_errors,
            *self.rate_limit_errors,
            *self.connection_errors,
            *self.status_errors,
        )

    def to_llm_error(self, error: BaseException) -> LLMError:
        """把 SDK 异常按类型归一成统一 LLMError；5xx 转可重试 ServerError，其余归一般 API 错误。"""
        if isinstance(error, self.authentication_errors):
            return AuthenticationError(f"Invalid API key: {error}")
        if isinstance(error, self.rate_limit_errors):
            return rate_limit_error(error)
        if isinstance(error, self.connection_errors):
            return NetworkError(f"Network error: {error}")
        if isinstance(error, self.status_errors):
            status_code = getattr(error, "status_code", "unknown")
            message = getattr(error, "message", str(error))
            # 5xx 是 provider 侧瞬时故障（可重试），4xx 归一般 API 错误
            if isinstance(status_code, int) and status_code >= 500:
                return ServerError(f"API error ({status_code}): {message}", status_code)
            return LLMError(f"API error ({status_code}): {message}")
        return LLMError(f"Provider error: {error}")


def provider_error_mapper(sdk: ModuleType) -> ProviderErrorMapper:
    """从具体 provider SDK 模块里抽取各类异常的元组，组装归一化映射器。"""
    return ProviderErrorMapper(
        authentication_errors=_exception_tuple(sdk.AuthenticationError),
        rate_limit_errors=_exception_tuple(sdk.RateLimitError),
        connection_errors=_exception_tuple(sdk.APIConnectionError),
        status_errors=_exception_tuple(sdk.APIStatusError),
    )
