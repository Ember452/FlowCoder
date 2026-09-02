"""容器运行时抽象：Protocol + Docker SDK 实现。

Docker SDK 是同步阻塞库，所有调用必须经 asyncio.to_thread 进入执行（AGENTS.md 异步纪律）。
单元测试注入 fake 实现本 Protocol，不依赖真实 Docker（既定决策，见 PROMPTS.md P1a）。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Protocol


class SandboxError(RuntimeError):
    """沙箱操作失败（SDK 未安装、daemon 不可用、容器创建/执行/清理出错）。"""


class ExecOutcome(NamedTuple):
    """一次 exec_run 的原始结果。"""

    exit_code: int
    stdout: str
    stderr: str


class ContainerRuntime(Protocol):
    """对 Docker SDK 的最小化抽象，只覆盖沙箱需要的九个操作。

    P1a 六个基本操作之外，P1b 池化追加三个：
    is_alive（租借健康检查）、list_by_label（孤儿容器清理）、stats（资源峰值采集）。
    """

    def create(self, spec: Mapping[str, Any]) -> str: ...

    def start(self, container_id: str) -> None: ...

    def put_archive(self, container_id: str, path: str, data: bytes) -> None: ...

    def exec_run(self, container_id: str, cmd: list[str], workdir: str) -> ExecOutcome: ...

    def kill(self, container_id: str, signal: str) -> None: ...

    def remove(self, container_id: str, *, force: bool) -> None: ...

    def is_alive(self, container_id: str) -> bool: ...

    def list_by_label(self, label: str) -> list[str]: ...

    def stats(self, container_id: str) -> dict[str, float]: ...


class DockerRuntime:
    """基于 docker SDK 的运行时实现，SDK 未安装或 daemon 不可用时构造失败。"""

    def __init__(self, client: Any, docker_module: Any) -> None:
        self._client = client
        self._docker = docker_module

    @classmethod
    def from_env(cls) -> DockerRuntime:
        try:
            import docker
        except ImportError as exc:
            raise SandboxError(
                "Docker SDK 未安装：请安装可选依赖 `pip install flowcoder[sandbox]`"
            ) from exc
        try:
            client = docker.from_env()
            client.ping()
        except docker.errors.DockerException as exc:
            raise SandboxError(f"Docker daemon 不可用：{exc}") from exc
        return cls(client, docker)

    def create(self, spec: Mapping[str, Any]) -> str:
        try:
            container = self._client.containers.create(**spec)
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器创建失败：{exc}") from exc
        return str(container.id)

    def start(self, container_id: str) -> None:
        try:
            self._client.containers.get(container_id).start()
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器启动失败：{exc}") from exc

    def put_archive(self, container_id: str, path: str, data: bytes) -> None:
        try:
            self._client.containers.get(container_id).put_archive(path, data)
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"文件传入失败：{exc}") from exc

    def exec_run(self, container_id: str, cmd: list[str], workdir: str) -> ExecOutcome:
        try:
            code, output = self._client.containers.get(container_id).exec_run(
                cmd=cmd, workdir=workdir, demux=True
            )
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器内执行失败：{exc}") from exc
        stdout, stderr = output
        return ExecOutcome(int(code), _decode(stdout), _decode(stderr))

    def kill(self, container_id: str, signal: str) -> None:
        try:
            self._client.containers.get(container_id).kill(signal=signal)
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器 kill 失败：{exc}") from exc

    def remove(self, container_id: str, *, force: bool) -> None:
        try:
            self._client.containers.get(container_id).remove(force=force)
        except self._docker.errors.NotFound:
            # 已被外部删除时 close() 仍应幂等成功
            return
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器删除失败：{exc}") from exc

    def is_alive(self, container_id: str) -> bool:
        try:
            container = self._client.containers.get(container_id)
            container.reload()
        except self._docker.errors.NotFound:
            return False
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器状态查询失败：{exc}") from exc
        return getattr(container, "status", "running") == "running"

    def list_by_label(self, label: str) -> list[str]:
        try:
            containers = self._client.containers.list(filters={"label": label})
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器列表查询失败：{exc}") from exc
        return [str(c.id) for c in containers]

    def stats(self, container_id: str) -> dict[str, float]:
        """一次性采样容器资源占用：memory_mb 与 cpu_percent。"""
        try:
            raw = self._client.containers.get(container_id).stats(stream=False)
        except self._docker.errors.DockerException as exc:
            raise SandboxError(f"容器资源统计失败：{exc}") from exc
        memory_bytes = (raw.get("memory_stats") or {}).get("usage") or 0
        cpu_stats = raw.get("cpu_stats") or {}
        precpu = raw.get("precpu_stats") or {}
        cpu_delta = (cpu_stats.get("cpu_usage") or {}).get("total_usage") or 0
        cpu_delta -= (precpu.get("cpu_usage") or {}).get("total_usage") or 0
        system_delta = (cpu_stats.get("system_cpu_usage") or 0) - (
            precpu.get("system_cpu_usage") or 0
        )
        online_cpus = cpu_stats.get("online_cpus") or 1
        # Docker 的 CPU% 按两帧间的 CPU 增量占比换算：delta/system_delta × 核数 × 100
        cpu_percent = cpu_delta / system_delta * online_cpus * 100 if system_delta > 0 else 0.0
        return {
            "memory_mb": memory_bytes / (1024 * 1024),
            "cpu_percent": cpu_percent,
        }


def _decode(data: bytes | None) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")
