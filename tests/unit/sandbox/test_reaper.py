"""LeaseReaper 的孤儿对账回收测试（fake 运行时与任务存活性回调）。"""

from __future__ import annotations

import asyncio
import time

import pytest

from flowcoder.sandbox.pool import SandboxPool
from flowcoder.sandbox.reaper import LeaseReaper
from flowcoder.sandbox.runtime import SandboxError

from conftest import FakeRuntime


async def _drain() -> None:
    for _ in range(10):
        await asyncio.sleep(0.01)


async def _eventually(condition, timeout_s: float = 5.0) -> None:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("条件在超时前未满足")


@pytest.fixture
def pool(fake_runtime: FakeRuntime) -> SandboxPool:
    return SandboxPool(size=2, runtime=fake_runtime)


def _reaper(pool: SandboxPool, running: set[str]) -> LeaseReaper:
    return LeaseReaper(pool, is_task_running=lambda tid: tid in running, interval_s=1.0)


class TestReconcile:
    async def test_orphan_reaped_when_task_dead(
        self, pool: SandboxPool, fake_runtime: FakeRuntime
    ) -> None:
        await pool.start()
        lease = await pool.lease(task_id="task-a")
        cid = lease.container.container_id
        reaper = _reaper(pool, running=set())  # task-a 已不在运行

        reaped = await reaper.reconcile_once()
        assert reaped == [cid]
        assert (cid, True) in fake_runtime.removed
        assert pool.snapshot()["active"] == 0
        await _eventually(lambda: len(fake_runtime.create_specs) == 3)  # 回收后补建

    async def test_live_task_lease_untouched(
        self, pool: SandboxPool, fake_runtime: FakeRuntime
    ) -> None:
        await pool.start()
        await pool.lease(task_id="task-a")
        reaper = _reaper(pool, running={"task-a"})

        assert await reaper.reconcile_once() == []
        assert fake_runtime.removed == []
        assert pool.snapshot()["active"] == 1

    async def test_lease_without_task_id_never_reaped(
        self, pool: SandboxPool, fake_runtime: FakeRuntime
    ) -> None:
        await pool.start()
        await pool.lease()
        reaper = _reaper(pool, running=set())

        assert await reaper.reconcile_once() == []
        assert fake_runtime.removed == []

    async def test_reap_remove_failure_swallowed(
        self, pool: SandboxPool, fake_runtime: FakeRuntime
    ) -> None:
        await pool.start()
        await pool.lease(task_id="task-a")
        original_remove = fake_runtime.remove

        def broken_remove(container_id: str, *, force: bool) -> None:
            raise SandboxError("daemon 抽风")

        fake_runtime.remove = broken_remove  # type: ignore[method-assign]
        reaper = _reaper(pool, running=set())
        reaped = await reaper.reconcile_once()  # 不抛，仅记日志
        assert len(reaped) == 1
        assert pool.snapshot()["active"] == 0
        fake_runtime.remove = original_remove  # type: ignore[method-assign]


class TestLifecycle:
    def test_rejects_zero_interval(self, pool: SandboxPool) -> None:
        with pytest.raises(ValueError):
            LeaseReaper(pool, is_task_running=lambda t: True, interval_s=0)

    async def test_run_forever_reconciles_periodically(
        self, pool: SandboxPool, fake_runtime: FakeRuntime
    ) -> None:
        await pool.start()
        lease = await pool.lease(task_id="task-a")
        reaper = LeaseReaper(pool, is_task_running=lambda t: False, interval_s=0.05)
        runner = asyncio.create_task(reaper.run_forever())
        await asyncio.sleep(0.2)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        assert (lease.container.container_id, True) in fake_runtime.removed
