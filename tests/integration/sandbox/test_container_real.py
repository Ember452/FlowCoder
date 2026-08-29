"""真实 Docker 容器的集成测试。

无 Docker 环境（SDK 未装或 daemon 不可达）自动跳过，不 fail——P1a 既定决策，
真实容器验收待环境就绪后补测。
"""

from __future__ import annotations

import pytest

from flowcoder.sandbox.container import SandboxContainer
from flowcoder.sandbox.runtime import DockerRuntime, SandboxError


def _docker_available() -> bool:
    try:
        DockerRuntime.from_env()
    except SandboxError:
        return False
    return True


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="本机无 Docker daemon"),
]


@pytest.fixture
async def container() -> SandboxContainer:
    c = SandboxContainer()
    await c.start()
    yield c
    await c.close()


class TestRealContainer:
    async def test_normal_execution(self, container: SandboxContainer) -> None:
        result = await container.execute("echo hello-sandbox", files={"greet.py": "print('py ok')"})
        assert result.exit_code == 0
        assert "hello-sandbox" in result.stdout
        assert not result.timed_out

    async def test_python_script_from_copied_file(self, container: SandboxContainer) -> None:
        result = await container.execute(
            ["python", "greet.py"], files={"greet.py": "print('py ok')"}
        )
        assert result.exit_code == 0
        assert "py ok" in result.stdout

    async def test_timeout_killed(self, container: SandboxContainer) -> None:
        result = await container.execute("sleep 60", timeout_s=2.0, kill_grace_s=1.0)
        assert result.timed_out
        assert result.exit_code in (124, 137, None)  # timeout TERM=124 / KILL=137

    async def test_network_isolated_by_default(self, container: SandboxContainer) -> None:
        # network_mode=none：容器内除 loopback 外无接口，curl 连 loopback 上的假端口立即失败
        result = await container.execute(
            "python -c \"import socket; socket.create_connection(('10.255.255.1', 80), timeout=2)\"",
            timeout_s=5.0,
        )
        assert result.exit_code != 0

    async def test_memory_limit_kills_hog(self, container: SandboxContainer) -> None:
        result = await container.execute(
            'python -c "x = bytearray(1024 * 1024 * 1024)"', timeout_s=20.0
        )
        assert result.exit_code not in (0, None)

    async def test_file_cannot_escape_workdir(self, container: SandboxContainer) -> None:
        # 只读根文件系统：工作目录外不可写
        result = await container.execute("touch /etc/evil")
        assert result.exit_code != 0
