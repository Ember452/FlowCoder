"""Provider 鉴权辅助。

识别本地 base_url、解析 OpenAI API Key 等。"""

from __future__ import annotations

from urllib.parse import urlparse

from flowcoder.config import ProviderConfig

LOCAL_PROVIDER_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCAL_OPENAI_API_KEY = "flowcoder-local"


def is_local_base_url(base_url: str) -> bool:
    """判断 base_url 是否指向本机（127.0.0.1/localhost/::1），用于识别本地 provider。"""
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:
        return False
    return host.lower() in LOCAL_PROVIDER_HOSTS


def resolve_openai_api_key(config: ProviderConfig) -> str:
    """解析 OpenAI API Key；本机 provider 无需真实密钥，用占位符代替。"""
    api_key = config.resolve_api_key()
    if api_key:
        return api_key
    if is_local_base_url(config.base_url):
        return LOCAL_OPENAI_API_KEY
    return ""
