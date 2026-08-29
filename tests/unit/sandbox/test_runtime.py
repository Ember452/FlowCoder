"""DockerRuntime（真实 docker SDK 适配层）的行为测试：注入 fake docker 模块。

from_env 的"SDK 未安装 / daemon 不可用"两条失败路径也在此覆盖。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from flowcoder.sandbox.runtime import (
    DockerRuntime,
    ExecOutcome,
    SandboxError,
)


class FakeDockerErrors:
    class DockerException(Exception): ...

    class NotFound(DockerException): ...


class FakeContainer:
    def __init__(self, parent: FakeDockerClient, cid: str) -> None:
        self._parent = parent
        self.id = cid
        self.status = "running"

    def reload(self) -> None:
        if self.id in self._parent.dead:
            self.status = "exited"

    def start(self) -> None:
        self._parent.calls.append(("start", self.id))

    def put_archive(self, path: str, data: bytes) -> None:
        self._parent.calls.append(("put_archive", self.id, path, data))

    def exec_run(self, *, cmd: list[str], workdir: str, demux: bool) -> Any:
        self._parent.calls.append(("exec_run", self.id, cmd, workdir, demux))
        return self._parent.exec_result

    def kill(self, signal: str) -> None:
        self._parent.calls.append(("kill", self.id, signal))

    def remove(self, force: bool) -> None:
        if self.id in self._parent.removed_externally:
            raise FakeDockerErrors.NotFound("already gone")
        self._parent.calls.append(("remove", self.id, force))

    def stats(self, *, stream: bool) -> Any:
        return self._parent.stats_result


class FakeContainers:
    def __init__(self, parent: FakeDockerClient) -> None:
        self._parent = parent

    def create(self, **spec: Any) -> FakeContainer:
        self._parent.calls.append(("create", spec))
        return FakeContainer(self._parent, "cid-abc")

    def get(self, container_id: str) -> FakeContainer:
        return FakeContainer(self._parent, container_id)

    def list(self, *, filters: dict[str, Any]) -> list[FakeContainer]:
        self._parent.calls.append(("list", filters))
        return [FakeContainer(self._parent, cid) for cid in self._parent.listed_ids]


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers(self)
        self.calls: list[Any] = []
        self.exec_result: Any = (0, (b"out", b"err"))
        self.removed_externally: set[str] = set()
        self.dead: set[str] = set()
        self.listed_ids: list[str] = []
        self.stats_result: Any = {
            "memory_stats": {"usage": 64 * 1024 * 1024},
            "cpu_stats": {
                "cpu_usage": {"total_usage": 2_000_000},
                "system_cpu_usage": 1_000_000_000,
                "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 1_000_000},
                "system_cpu_usage": 900_000_000,
            },
        }

    def ping(self) -> None: ...


class FakeDockerModule:
    errors = FakeDockerErrors

    @staticmethod
    def from_env() -> FakeDockerClient:
        return FakeDockerClient()


@pytest.fixture
def runtime() -> DockerRuntime:
    return DockerRuntime(FakeDockerClient(), FakeDockerModule)


class TestDockerRuntime:
    def test_create_returns_container_id(self, runtime: DockerRuntime) -> None:
        cid = runtime.create({"image": "python:3.11-slim"})
        assert cid == "cid-abc"

    def test_exec_run_decodes_demux_output(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.exec_result = (3, (b"hello", b"oops"))
        outcome = runtime.exec_run("cid", ["sh", "-c", "x"], "/workspace")
        assert outcome == ExecOutcome(3, "hello", "oops")

    def test_exec_run_none_streams_become_empty(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.exec_result = (0, (None, None))
        outcome = runtime.exec_run("cid", ["true"], "/workspace")
        assert outcome == ExecOutcome(0, "", "")

    def test_remove_tolerates_externally_deleted(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.removed_externally.add("cid")
        runtime.remove("cid", force=True)  # 不应抛出

    def test_docker_exception_wrapped_as_sandbox_error(self, runtime: DockerRuntime) -> None:
        class BrokenClient(FakeDockerClient):
            def ping(self) -> None:
                raise FakeDockerErrors.DockerException("boom")

        broken = DockerRuntime(BrokenClient(), FakeDockerModule)

        # create 走 containers.create，不会 ping；用一个必然失败的操作验证包装
        class ExplodeContainers(FakeContainers):
            def create(self, **spec: Any) -> FakeContainer:
                raise FakeDockerErrors.DockerException("nope")

        broken._client.containers = ExplodeContainers(broken._client)
        with pytest.raises(SandboxError, match="容器创建失败"):
            broken.create({"image": "x"})

    def test_non_utf8_bytes_replaced(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.exec_result = (0, (b"a\xffb", None))
        outcome = runtime.exec_run("cid", ["x"], "/workspace")
        assert "\ufffd" in outcome.stdout

    def test_is_alive_true_when_running(self, runtime: DockerRuntime) -> None:
        assert runtime.is_alive("cid") is True

    def test_is_alive_false_when_exited(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.dead.add("cid")
        assert runtime.is_alive("cid") is False

    def test_list_by_label_returns_ids(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.listed_ids = ["a", "b"]
        assert runtime.list_by_label("flowcoder.sandbox") == ["a", "b"]
        assert client.calls[-1] == ("list", {"label": "flowcoder.sandbox"})

    def test_stats_parses_memory_and_cpu(self, runtime: DockerRuntime) -> None:
        usage = runtime.stats("cid")
        assert usage["memory_mb"] == 64.0
        # cpu_delta=1_000_000 / system_delta=100_000_000 * 2 核 * 100 = 2%
        assert abs(usage["cpu_percent"] - 2.0) < 1e-6

    def test_stats_zero_when_no_system_delta(self, runtime: DockerRuntime) -> None:
        client: FakeDockerClient = runtime._client  # type: ignore[attr-defined]
        client.stats_result = {"memory_stats": {"usage": 0}, "cpu_stats": {}, "precpu_stats": {}}
        usage = runtime.stats("cid")
        assert usage["cpu_percent"] == 0.0


class TestFromEnv:
    def test_missing_sdk_raises_sandbox_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # sys.modules 置 None 会让 `import docker` 抛 ImportError，模拟 SDK 未安装
        monkeypatch.setitem(sys.modules, "docker", None)
        with pytest.raises(SandboxError, match="Docker SDK 未安装"):
            DockerRuntime.from_env()

    def test_unreachable_daemon_raises_sandbox_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class NoDaemonModule(FakeDockerModule):
            @staticmethod
            def from_env() -> FakeDockerClient:
                raise FakeDockerErrors.DockerException("connection refused")

        monkeypatch.setitem(sys.modules, "docker", NoDaemonModule)
        with pytest.raises(SandboxError, match="Docker daemon 不可用"):
            DockerRuntime.from_env()
