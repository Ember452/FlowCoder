"""Bash 工具 sandbox_mode 双通道与 /sandbox 命令的测试。

docker 执行路径全部用 fake pool 验证（不依赖真实 Docker，既定决策）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowcoder.commands.handlers.sandbox import handle_sandbox
from flowcoder.sandbox import SandboxError
from flowcoder.sandbox.container import ExecutionResult
from flowcoder.tools.bash import Bash, Params


class FakePool:
    """SandboxPool 的最小 fake：execute 直接返回注入的结果。"""

    def __init__(
        self,
        result: ExecutionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, float]] = []
        self.closed = False
        self._result = result or ExecutionResult(exit_code=0, stdout="", stderr="", duration_ms=5)
        self._error = error

    async def start(self) -> None: ...

    async def execute(self, command: str, **kwargs: object) -> ExecutionResult:
        self.calls.append((command, float(kwargs.get("timeout_s", 0))))
        if self._error is not None:
            raise self._error
        return self._result

    async def close(self) -> None:
        self.closed = True


def _docker_bash(pool: FakePool) -> Bash:
    bash = Bash(host_workdir="/tmp/proj")
    bash._sandbox_mode = "docker"
    bash._pool = pool
    return bash


class TestOffPathUnchanged:
    async def test_default_mode_is_off(self) -> None:
        assert Bash().sandbox_mode == "off"

    async def test_subprocess_echo(self) -> None:
        # 用最朴素的 echo 锁定 off 路径行为未变（P1c 验收：off 走原路径零改动）
        bash = Bash()
        result = await bash.execute(Params(command="echo hello-off"))
        assert "hello-off" in result.output
        assert not result.is_error


class TestDockerPath:
    async def test_output_formatting_and_exit_code(self) -> None:
        pool = FakePool(ExecutionResult(exit_code=0, stdout="out\n", stderr="err\n", duration_ms=9))
        result = await _docker_bash(pool).execute(Params(command="echo x"))
        assert "STDOUT:\nout" in result.output
        assert "STDERR:\nerr" in result.output
        assert not result.is_error
        assert pool.calls[0][0] == "echo x"

    async def test_nonzero_exit_is_error(self) -> None:
        pool = FakePool(ExecutionResult(exit_code=3, stdout="", stderr="", duration_ms=1))
        result = await _docker_bash(pool).execute(Params(command="false"))
        assert result.is_error

    async def test_grep_exit_1_not_error(self) -> None:
        # 退出码语义映射在 docker 通道同样生效
        pool = FakePool(ExecutionResult(exit_code=1, stdout="", stderr="", duration_ms=1))
        result = await _docker_bash(pool).execute(Params(command="cat f | grep nope"))
        assert not result.is_error

    async def test_timeout_reported(self) -> None:
        pool = FakePool(
            ExecutionResult(exit_code=None, stdout="", stderr="", duration_ms=60000, timed_out=True)
        )
        result = await _docker_bash(pool).execute(Params(command="sleep 999"))
        assert result.is_error
        assert "timed out" in result.output

    async def test_sandbox_error_is_error_result(self) -> None:
        pool = FakePool(error=SandboxError("Docker daemon 不可用"))
        result = await _docker_bash(pool).execute(Params(command="echo x"))
        assert result.is_error
        assert "Docker daemon 不可用" in result.output


class TestSetSandboxMode:
    async def test_unknown_mode_rejected(self) -> None:
        bash = Bash()
        error = await bash.set_sandbox_mode("k8s")
        assert error is not None
        assert bash.sandbox_mode == "off"

    async def test_same_mode_noop(self) -> None:
        bash = Bash()
        assert await bash.set_sandbox_mode("off") is None

    async def test_switch_to_off_closes_pool(self) -> None:
        pool = FakePool()
        bash = Bash()
        bash._sandbox_mode = "docker"
        bash._pool = pool
        assert await bash.set_sandbox_mode("off") is None
        assert bash.sandbox_mode == "off"
        assert pool.closed
        assert bash._pool is None

    async def test_switch_to_docker_failure_keeps_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 模拟 Docker 未安装：SandboxPool.start 抛 SandboxError，模式必须保持 off
        class BrokenPool:
            async def start(self) -> None:
                raise SandboxError("Docker SDK 未安装：请安装可选依赖")

        def broken_pool_factory(*args: object, **kwargs: object) -> BrokenPool:
            return BrokenPool()

        monkeypatch.setattr("flowcoder.sandbox.SandboxPool", broken_pool_factory)
        bash = Bash(host_workdir="/tmp/proj")
        error = await bash.set_sandbox_mode("docker")
        assert error is not None
        assert "Docker SDK 未安装" in error
        assert bash.sandbox_mode == "off"  # 未静默切换


class TestRegistryWiring:
    def test_initial_mode_from_factory(self) -> None:
        from flowcoder.tools import create_default_registry

        registry = create_default_registry(base_dir="/tmp/p", sandbox_mode="docker")
        bash = registry.get("Bash")
        assert bash is not None
        assert bash.sandbox_mode == "docker"

    def test_default_factory_off(self) -> None:
        from flowcoder.tools import create_default_registry

        registry = create_default_registry()
        bash = registry.get("Bash")
        assert bash is not None
        assert bash.sandbox_mode == "off"


# ------------------------------------------------------------------ /sandbox


class _RecorderUI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def send_user_message(self, text: str) -> None: ...

    def set_plan_mode(self, enabled: bool) -> None: ...

    def get_token_count(self) -> tuple[int, int]:
        return (0, 0)

    def refresh_status(self) -> None: ...


class _StubRegistry:
    def __init__(self, bash: Bash) -> None:
        self._bash = bash

    def get(self, name: str) -> object | None:
        return self._bash if name == "Bash" else None


class _StubAgent:
    def __init__(self, bash: Bash) -> None:
        self.work_dir = "/tmp/proj"
        self.registry = _StubRegistry(bash)


def _make_ctx(agent: _StubAgent, args: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from flowcoder.commands.registry import CommandContext

    recorded: list[tuple[str, str]] = []

    def fake_update(key: str, value: str, *, path: Path | None = None) -> Path:
        recorded.append((key, value))
        return tmp_path / "config.yaml"

    monkeypatch.setattr("flowcoder.commands.handlers.sandbox.update_user_config_value", fake_update)
    ctx = CommandContext(
        args=args,
        agent=agent,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=_RecorderUI(),
        config=None,
    )
    return ctx, recorded


@pytest.fixture
def stub_agent() -> _StubAgent:
    bash = Bash(host_workdir="/tmp/proj")
    bash._pool = FakePool()
    return _StubAgent(bash)


class TestSandboxCommand:
    async def test_status_shows_mode(
        self, stub_agent: _StubAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ctx, recorded = _make_ctx(stub_agent, "", monkeypatch, tmp_path)
        await handle_sandbox(ctx)
        assert any("当前模式: off" in m for m in ctx.ui.messages)
        assert recorded == []

    async def test_switch_persists(
        self, stub_agent: _StubAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ctx, recorded = _make_ctx(stub_agent, "docker", monkeypatch, tmp_path)
        await handle_sandbox(ctx)
        assert stub_agent.registry._bash.sandbox_mode == "docker"
        assert recorded == [("sandbox_mode", "docker")]
        assert any("已持久化" in m for m in ctx.ui.messages)

    async def test_switch_failure_keeps_mode_and_no_persist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bash = Bash(host_workdir="/tmp/proj")

        class _Agent:
            work_dir = "/tmp/proj"

            class registry:  # noqa: N801
                @staticmethod
                def get(name: str) -> object | None:
                    return bash if name == "Bash" else None

        async def failing_set(mode: str) -> str | None:
            return "无法启用 docker 沙箱模式：Docker daemon 不可达"

        monkeypatch.setattr(bash, "set_sandbox_mode", failing_set)
        ctx, recorded = _make_ctx(_Agent(), "docker", monkeypatch, tmp_path)
        await handle_sandbox(ctx)
        assert recorded == []  # 切换失败不持久化
        assert any("无法启用" in m for m in ctx.ui.messages)

    async def test_unknown_arg_shows_usage(
        self, stub_agent: _StubAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ctx, recorded = _make_ctx(stub_agent, "k8s", monkeypatch, tmp_path)
        await handle_sandbox(ctx)
        assert any("用法" in m for m in ctx.ui.messages)
        assert recorded == []
