"""沙箱容器：单容器执行入口（P1a，无池化）。

安全默认：non-root 用户、只读根文件系统（可写仅限 tmpfs 工作目录与 /tmp）、
默认断网、容器级资源限额；不挂载任何宿主目录。
双层超时：容器内 timeout 命令（先 SIGTERM、--kill-after 后 SIGKILL）+
asyncio 外层 wait_for 兜底（同样 TERM→KILL），两层各自独立生效。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from flowcoder.sandbox import transport
from flowcoder.sandbox.limits import ResourceLimits
from flowcoder.sandbox.limits import container_kwargs as limit_kwargs
from flowcoder.sandbox.network import container_kwargs as network_kwargs
from flowcoder.sandbox.runtime import (
    ContainerRuntime,
    DockerRuntime,
    SandboxError,
)

#: python:3.11-slim 自带的非 root 账户（nobody）
NON_ROOT_USER = "65534:65534"

#: 外层超时相对"内层超时 + kill 宽限"的追加余量，覆盖 docker exec 自身开销
EXEC_MARGIN_S = 5.0


@dataclass(frozen=True)
class SandboxConfig:
    """容器创建参数。"""

    image: str = "python:3.11-slim"
    workdir: str = "/workspace"
    user: str = NON_ROOT_USER
    limits: ResourceLimits = ResourceLimits()
    network_enabled: bool = False
    #: docker labels；池用它标记归属，reaper 据此清理遗留容器
    labels: dict[str, str] = field(default_factory=dict)
    #: 宿主目录 → 容器路径的读写挂载。默认空（不可信代码执行零暴露）；
    #: bash 工具 docker 模式用它把白名单工作目录映射进容器（P1c，挂载面
    #: 由权限门 + 白名单收窄，取舍见 docs/specs P1c ADR）
    mounts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    """一次沙箱执行的结果。"""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


def _wrap_with_inner_timeout(
    command: str | list[str], timeout_s: float, kill_grace_s: float
) -> list[str]:
    """内层超时：容器内 timeout 命令，SIGTERM 后 --kill-after 补 SIGKILL。"""
    prefix = [
        "timeout",
        "--kill-after",
        f"{kill_grace_s:.3f}s",
        f"{timeout_s:.3f}s",
    ]
    if isinstance(command, str):
        return [*prefix, "sh", "-c", command]
    return [*prefix, *command]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class SandboxContainer:
    """单个沙箱容器的生命周期与执行入口。"""

    def __init__(
        self,
        config: SandboxConfig | None = None,
        runtime: ContainerRuntime | None = None,
    ) -> None:
        self._config = config or SandboxConfig()
        self._runtime = runtime
        self._container_id: str | None = None
        self._closed = False

    async def start(self) -> None:
        """创建并启动容器，记录执行的 ID；已启动时幂等直接返回。"""
        if self._closed:
            raise SandboxError("容器已关闭，不能再启动")
        if self._container_id is not None:
            return
        runtime = self._ensure_runtime()
        spec = self._build_create_spec()
        container_id = await asyncio.to_thread(runtime.create, spec)
        await asyncio.to_thread(runtime.start, container_id)
        self._container_id = container_id

    async def execute(
        self,
        command: str | list[str],
        *,
        files: Mapping[str, bytes | str] | None = None,
        timeout_s: float = 30.0,
        kill_grace_s: float = 2.0,
    ) -> ExecutionResult:
        """在容器内执行命令；files 先经 transport 传入工作目录。"""
        if self._closed:
            raise SandboxError("容器已关闭")
        if self._container_id is None:
            raise SandboxError("容器未启动：先调用 start()")
        if timeout_s <= 0 or kill_grace_s < 0:
            raise ValueError("timeout_s 必须为正数，kill_grace_s 不能为负")

        runtime = self._ensure_runtime()
        container_id = self._container_id
        if files:
            await transport.copy_files(runtime, container_id, self._config.workdir, files)
        cmd = _wrap_with_inner_timeout(command, timeout_s, kill_grace_s)

        started = time.perf_counter()
        try:
            outcome = await asyncio.wait_for(
                asyncio.to_thread(runtime.exec_run, container_id, cmd, self._config.workdir),
                timeout=timeout_s + kill_grace_s + EXEC_MARGIN_S,
            )
        except TimeoutError:
            # 内层 timeout 没能收场（exec 自身挂死等），外层对容器 TERM→KILL 兜底
            await self._kill_after_grace(runtime, container_id, kill_grace_s)
            return ExecutionResult(
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=_elapsed_ms(started),
                timed_out=True,
            )
        return ExecutionResult(
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_ms=_elapsed_ms(started),
        )

    async def close(self) -> None:
        """强删容器，幂等。"""
        if self._container_id is None:
            return
        runtime = self._ensure_runtime()
        container_id = self._container_id
        self._container_id = None
        self._closed = True
        await asyncio.to_thread(runtime.remove, container_id, force=True)

    async def _kill_after_grace(
        self, runtime: ContainerRuntime, container_id: str, grace_s: float
    ) -> None:
        """外层兜底：SIGTERM 先行，宽限后再 SIGKILL。"""
        try:
            await asyncio.to_thread(runtime.kill, container_id, "SIGTERM")
            await asyncio.sleep(grace_s)
            await asyncio.to_thread(runtime.kill, container_id, "SIGKILL")
        except SandboxError:
            # 容器已自行退出/已被删除时 kill 会报错，"无残留"的目标已达成
            pass

    def _build_create_spec(self) -> dict[str, Any]:
        """组装容器创建参数：只读根、tmpfs 工作目录、断网与限额、可写挂载。"""
        cfg = self._config
        spec: dict[str, Any] = {
            "image": cfg.image,
            "command": ["sleep", "infinity"],
            "user": cfg.user,
            "working_dir": cfg.workdir,
            "read_only": True,
            "tmpfs": {cfg.workdir: "mode=1777", "/tmp": "mode=1777"},
        }
        spec.update(limit_kwargs(cfg.limits))
        spec.update(network_kwargs(cfg.network_enabled))
        if cfg.labels:
            spec["labels"] = dict(cfg.labels)
        if cfg.mounts:
            spec["volumes"] = {
                host: {"bind": container_path, "mode": "rw"}
                for host, container_path in cfg.mounts.items()
            }
        return spec

    @property
    def container_id(self) -> str | None:
        """当前容器 ID；未启动或已关闭时为 None。"""
        return self._container_id

    def _ensure_runtime(self) -> ContainerRuntime:
        if self._runtime is None:
            self._runtime = DockerRuntime.from_env()
        return self._runtime
