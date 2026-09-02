"""云端账号 API 客户端（同步）。

对接：
  POST /api/auth/login|register
  GET  /api/models
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from flowcoder.account.session import AccountSession, DEFAULT_BASE_URL


class AccountClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CatalogModel:
    name: str
    display_name: str = ""
    provider: str = ""
    protocol: str = "openai-compat"
    model_id: str = ""
    thinking: bool = False

    def label(self) -> str:
        return self.display_name or self.name


def normalize_base_url(base_url: str | None) -> str:
    """规范化云端 base_url：去空白/尾部斜杠，空值回退默认地址。"""
    value = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not value:
        raise AccountClientError("base_url required")
    return value


def _error_message(payload: Any, fallback: str) -> str:
    # 从响应体提取服务端返回的 error/message 作为可读错误文案，否则用 HTTP 状态回退
    if isinstance(payload, dict):
        err = payload.get("error") or payload.get("message")
        if isinstance(err, str) and err.strip():
            return err.strip()
    return fallback


def _request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    """发起带可选 Bearer 认证的同步请求，统一把 HTTP/网络错误折叠为 AccountClientError。"""
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise AccountClientError(f"cloud request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        raise AccountClientError(
            _error_message(payload, f"HTTP {response.status_code}"),
            status_code=response.status_code,
        )
    return payload


def login(
    *,
    email: str,
    password: str,
    base_url: str | None = None,
    register: bool = False,
) -> AccountSession:
    """登录（或注册）云端账号，用返回的 JWT 构造 AccountSession。"""
    root = normalize_base_url(base_url)
    path = "/api/auth/register" if register else "/api/auth/login"
    payload = _request_json(
        "POST",
        f"{root}{path}",
        json_body={"email": email.strip(), "password": password},
    )
    if not isinstance(payload, dict):
        raise AccountClientError("invalid auth response")
    token = str(payload.get("token") or "").strip()
    if not token:
        raise AccountClientError("auth response missing token")
    user_id_raw = payload.get("user_id")
    user_id: int | None
    if isinstance(user_id_raw, int) and not isinstance(user_id_raw, bool):
        user_id = user_id_raw
    else:
        try:
            user_id = int(user_id_raw) if user_id_raw is not None else None
        except (TypeError, ValueError):
            user_id = None
    return AccountSession(
        base_url=root,
        token=token,
        email=str(payload.get("email") or email).strip(),
        role=str(payload.get("role") or "user").strip() or "user",
        user_id=user_id,
        selected_model="",
    )


def fetch_models(session: AccountSession) -> list[CatalogModel]:
    """拉取当前账号可见的模型目录并转为 CatalogModel 列表。"""
    payload = _request_json(
        "GET",
        f"{session.base_url.rstrip('/')}/api/models",
        token=session.token,
    )
    if not isinstance(payload, dict):
        raise AccountClientError("invalid models response")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return []
    models: list[CatalogModel] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        models.append(
            CatalogModel(
                name=name,
                display_name=str(item.get("display_name") or name).strip(),
                provider=str(item.get("provider") or item.get("provider_name") or "").strip(),
                protocol=str(item.get("protocol") or "openai-compat").strip() or "openai-compat",
                model_id=str(item.get("model_id") or "").strip(),
                thinking=bool(item.get("thinking", False)),
            )
        )
    return models
