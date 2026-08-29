"""网络策略测试：默认断网是安全默认值。"""

from __future__ import annotations

from flowcoder.sandbox.network import container_kwargs


def test_default_is_isolated() -> None:
    # network_enabled=False（默认）必须给 network_mode=none，容器内除 loopback 外无接口
    assert container_kwargs(False) == {"network_mode": "none"}


def test_enabled_keeps_docker_default() -> None:
    assert container_kwargs(True) == {}
