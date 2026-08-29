"""网络策略：默认断网。

白名单域名走代理是 TRANSFORMATION_PLAN Phase 1 后续能力，本阶段只提供全断。
network_mode=none 是 Docker 的强隔离：容器内除 loopback 外没有任何网络接口。
"""

from __future__ import annotations

from typing import Any


def container_kwargs(network_enabled: bool) -> dict[str, Any]:
    """断网（默认）或保持 Docker 默认网络的容器创建参数。"""
    if network_enabled:
        return {}
    return {"network_mode": "none"}
