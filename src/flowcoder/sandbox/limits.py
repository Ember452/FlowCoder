"""容器级 cgroup 资源限额（--memory / --cpus / pids-limit）。

执行级超时由 SandboxContainer 的双层超时负责，不在本模块；
两层合起来构成"容器级 + 执行级"双层限额（ADR 见 docs/specs/）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceLimits:
    """容器级资源上限。"""

    memory_mb: int = 256
    cpus: float = 1.0
    pids_limit: int = 128

    def __post_init__(self) -> None:
        if self.memory_mb <= 0:
            raise ValueError("memory_mb 必须为正数")
        if self.cpus <= 0:
            raise ValueError("cpus 必须为正数")
        if self.pids_limit <= 0:
            raise ValueError("pids_limit 必须为正数")


def container_kwargs(limits: ResourceLimits) -> dict[str, Any]:
    """把限额翻译成 docker containers.create 的关键字参数。"""
    return {
        "mem_limit": f"{limits.memory_mb}m",
        "nano_cpus": int(limits.cpus * 1_000_000_000),
        "pids_limit": limits.pids_limit,
    }
