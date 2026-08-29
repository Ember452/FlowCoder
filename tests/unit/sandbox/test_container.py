"""SandboxContainer 的生命周期、双层超时与执行入口测试（全部基于 fake 运行时）。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from flowcoder.sandbox.container import (
    EXEC_MARGIN_S,
    SandboxConfig,
    SandboxContainer,
)
from flowcoder.sandbox.limits import ResourceLimits
from flowcoder.sandbox.runtime import ExecOutcome, SandboxError

if TYPE_CHECKING:
    # 仅类型标注用；运行时 FakeRuntime 由 conftest fixture 注入
    from conftest import FakeRuntime


@pytest.fixture
def container(fake_runtime: FakeRuntime) -> SandboxContainer:
    cfg = SandboxConfig(limits=ResourceLimits(memory_mb=128, cpus=0.5, pids_limit=32))
    return SandboxContainer(config=cfg, runtime=fake_runtime)


async def _start(container: SandboxContainer) -> FakeRuntime:
    await container.start()
    return container._runtime  # type: ignore[return-value]


class TestLifecycle:
    async def test_start_builds_secure_spec(self, container: SandboxContainer) -> None:
        await container.start()
        spec = container._runtime.create_specs[0]  # type: ignore[attr-defined]
        assert spec["image"] == "python:3.11-slim"
        assert spec["user"] == "65534:65534"
        assert spec["read_only"] is True
        assert spec["command"] == ["sleep", "infinity"]
        assert spec["network_mode"] == "none"
        assert spec["mem_limit"] == "128m"
        assert spec["nano_cpus"] == 500_000_000
        assert spec["pids_limit"] == 32
        assert "/workspace" in spec["tmpfs"]

    async def test_start_is_idempotent(self, container: SandboxContainer) -> None:
        await container.start()
        await container.start()
        rt: FakeRuntime = container._runtime  # type: ignore[assignment]
        assert len(rt.create_specs) == 1

    async def test_execute_before_start_raises(self, container: SandboxContainer) -> None:
        with pytest.raises(SandboxError, match="未启动"):
            await container.execute("echo hi")

    async def test_close_removes_forcefully(self, container: SandboxContainer) -> None:
        await container.start()
        await container.close()
        rt: FakeRuntime = container._runtime  # type: ignore[assignment]
        assert rt.removed == [("cid-0001", True)]

    async def test_close_without_start_is_noop(self, container: SandboxContainer) -> None:
        await container.close()
        rt: FakeRuntime = container._runtime  # type: ignore[assignment]
        assert rt.removed == []

    async def test_close_is_idempotent(self, container: SandboxContainer) -> None:
        await container.start()
        await container.close()
        await container.close()
        rt: FakeRuntime = container._runtime  # type: ignore[assignment]
        assert rt.removed == [("cid-0001", True)]

    async def test_reuse_after_close_rejected(self, container: SandboxContainer) -> None:
        await container.start()
        await container.close()
        with pytest.raises(SandboxError, match="已关闭"):
            await container.execute("echo hi")
        with pytest.raises(SandboxError, match="已关闭"):
            await container.start()


class TestExecute:
    async def test_normal_execution(self, container: SandboxContainer) -> None:
        rt = await _start(container)
        rt.exec_fn = lambda cid, cmd, wd: ExecOutcome(0, "hello\n", "")
        result = await container.execute("echo hello")
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert not result.timed_out

    async def test_command_wrapped_with_inner_timeout(self, container: SandboxContainer) -> None:
        rt = await _start(container)
        await container.execute("echo hi", timeout_s=10.0, kill_grace_s=1.0)
        cid, cmd, workdir = rt.exec_calls[0]
        assert cid == "cid-0001"
        assert workdir == "/workspace"
        # 内层 timeout：先 SIGTERM，kill-after 到点后 SIGKILL
        assert cmd[:4] == ["timeout", "--kill-after", "1.000s", "10.000s"]
        assert cmd[4:] == ["sh", "-c", "echo hi"]

    async def test_list_command_wrapped(self, container: SandboxContainer) -> None:
        rt = await _start(container)
        await container.execute(["python", "-c", "print(1)"])
        _, cmd, _ = rt.exec_calls[0]
        assert cmd[4:] == ["python", "-c", "print(1)"]

    async def test_files_copied_before_exec(self, container: SandboxContainer) -> None:
        rt = await _start(container)
        await container.execute("python main.py", files={"main.py": "print(1)"})
        assert len(rt.archives) == 1
        path, data = rt.archives[0]
        assert path == "/workspace"
        assert b"main.py" in data
        assert len(rt.exec_calls) == 1

    async def test_invalid_timeout_args(self, container: SandboxContainer) -> None:
        await container.start()
        with pytest.raises(ValueError):
            await container.execute("echo", timeout_s=0)
        with pytest.raises(ValueError):
            await container.execute("echo", kill_grace_s=-1)


class TestDoubleTimeout:
    async def test_outer_timeout_kills_after_grace(self, fake_runtime: FakeRuntime) -> None:
        # 内层挂死超过外层预算：外层 wait_for 兜底，对容器 TERM→KILL
        def hanging_exec(cid: str, cmd: list[str], wd: str) -> ExecOutcome:
            time.sleep(7)  # 模拟 exec 自身挂死（须超过 外层预算=0.3+0.2+5 余量）
            return ExecOutcome(0, "", "")

        fake_runtime.exec_fn = hanging_exec
        container = SandboxContainer(runtime=fake_runtime)
        await container.start()
        result = await container.execute("sleep 9999", timeout_s=0.3, kill_grace_s=0.2)
        assert result.timed_out
        assert result.exit_code is None
        assert fake_runtime.kill_signals == ["SIGTERM", "SIGKILL"]

    async def test_outer_timeout_budget_has_margin(self, container: SandboxContainer) -> None:
        # 外层预算 = 内层超时 + kill 宽限 + EXEC_MARGIN_S 余量，覆盖 docker exec 自身开销
        await container.start()
        await container.execute("echo", timeout_s=1.0, kill_grace_s=0.5)
        assert EXEC_MARGIN_S > 0
