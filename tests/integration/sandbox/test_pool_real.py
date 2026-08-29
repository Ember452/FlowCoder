"""真实 Docker 容器池的集成测试（无 Docker 自动跳过，待环境就绪回填 ADR）。"""

from __future__ import annotations

import asyncio

import pytest

from flowcoder.sandbox.pool import SANDBOX_LABEL, SandboxPool
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


class TestRealPool:
    async def test_concurrent_execution_and_rebuild(self) -> None:
        pool = SandboxPool(size=3)
        await pool.start()
        try:
            results = await asyncio.gather(
                *(pool.execute("echo batch", timeout_s=30.0) for _ in range(9))
            )
            assert all(r.exit_code == 0 for r in results)
            assert pool.snapshot()["leases"] == 9
            # 每次归还后补建，池水位恢复
            await asyncio.sleep(1.0)
            assert pool.snapshot()["idle"] == 3
        finally:
            await pool.close()

    async def test_killed_idle_container_replenished(self) -> None:
        pool = SandboxPool(size=2)
        await pool.start()
        try:
            # 模拟外部 kill：找到池的某个空闲容器强删
            import docker

            client = docker.from_env()
            victims = client.containers.list(filters={"label": SANDBOX_LABEL, "status": "running"})
            assert victims, "池应有运行中的容器"
            victims[0].remove(force=True)

            lease = await pool.lease()
            # 租借体检淘汰死容器，拿到的必是健康的
            result = await lease.execute("echo alive", timeout_s=30.0)
            assert result.exit_code == 0
            await lease.release()
        finally:
            await pool.close()

    async def test_startup_clears_abandoned_containers(self) -> None:
        # 先造一个带沙箱 label 的遗留容器，再启动池，验证被清理
        import docker

        client = docker.from_env()
        abandoned = client.containers.create(
            "python:3.11-slim",
            ["sleep", "infinity"],
            labels={SANDBOX_LABEL: "pool-abandoned"},
        )
        abandoned.start()
        try:
            pool = SandboxPool(size=1)
            await pool.start()
            remaining = client.containers.list(
                filters={"label": SANDBOX_LABEL, "status": "running"}
            )
            ids = {c.id for c in remaining}
            assert abandoned.id not in ids
            await pool.close()
        finally:
            abandoned.remove(force=True)
