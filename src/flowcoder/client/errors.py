"""共享 LLM 错误类型与规范化辅助。

AuthenticationError / RateLimitError / NetworkError 等。"""

from __future__ import annotations


class LLMError(Exception):
    """Base error for model provider failures."""


class AuthenticationError(LLMError):
    """Raised when provider credentials are missing or rejected."""


class RateLimitError(LLMError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(LLMError):
    """Raised when a provider request fails due to connectivity."""


class ServerError(LLMError):
    """Raised when the provider returns a 5xx server error (retryable)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LLMTimeoutError(LLMError):
    """Raised when a provider request exceeds the per-request timeout (retryable)."""


def response_header(error: BaseException, name: str) -> str:
    """安全地从 SDK 异常对象里取响应头某个值；对象无响应/取不到返回空串。"""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        return ""
    return str(value).strip() if value is not None else ""


def parse_retry_after_seconds(value: str) -> float | None:
    """把 Retry-After 头解析成等待秒数；空串/非法/负数都视为无建议。"""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def rate_limit_error(error: BaseException) -> RateLimitError:
    """把触发限流的原始异常转成 RateLimitError，并带上 provider 建议的 retry-after。"""
    retry_after = parse_retry_after_seconds(response_header(error, "retry-after"))
    if retry_after is None:
        return RateLimitError("Rate limited. Please wait.")
    return RateLimitError(
        f"Rate limited. Retry after {retry_after:g}s.",
        retry_after=retry_after,
    )
