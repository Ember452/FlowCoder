"""SandboxPool 的预热、租借、背压、销毁重建与指标测试（fake 运行时）。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

from flowcoder.sandbox.container import SandboxContainer
from flowcoder.sandbox.pool import (
    SANDBOX_LABEL,
    PoolExhaustedError,
    SandboxPool,
)
from flowcoder.sandbox.runtime import ExecOutcome, SandboxError

from conftest import FakeRuntime


@dataclass
class FakeTraceNode:
    agent_id: str


class FakeTraceSink:
    """结构上兼容 TraceManager 的 fake：记录 create/update/complete 调用。"""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.completed: list[tuple[str, str]] = []
        self._n = 0

    def create(
        self, agent_type: str, parent_id: str | None = None, trace_id: str | None = None
    ) -> FakeTraceNode:
        self.created.append(agent_type)
        self._n += 1
        return FakeTraceNode(agent_id=f"trace-{self._n}")

    def update(self, agent_id: str, **kwargs: int | str) -> None:
        self.updates.append((agent_id, dict(kwargs)))

    def complete(self, agent_id: str, status: str = "completed") -> None:
        self.completed.append((agent_id, status))


async def _drain() -> None:
    """让事件循环跑几圈，flush 纯本地状态的异步副作用。"""
    for _ in range(10):
        await asyncio.sleep(0.01)


async def _eventually(condition, timeout_s: float = 5.0) -> None:
    """轮询等待异步副作用（后台补建任务等）达成，替代固定 sleep 防负载抖动。"""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("条件在超时前未满足")


@pytest.fixture
def pool(fake_runtime: FakeRuntime) -> SandboxPool:
    return SandboxPool(size=3, runtime=fake_runtime)


class TestPreheatAndLease:
    async def test_start_preheats_and_labels(self, pool: SandboxPool) -> None:
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        assert len(rt.create_specs) == 3
        for spec in rt.create_specs:
            assert SANDBOX_LABEL in spec["labels"]

    async def test_lease_hands_out_idle_container(self, pool: SandboxPool) -> None:
        await pool.start()
        lease = await pool.lease()
        assert isinstance(lease.container, SandboxContainer)
        assert lease.container.container_id is not None
        assert pool.snapshot()["active"] == 1

    async def test_release_destroys_and_rebuilds(self, pool: SandboxPool) -> None:
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        lease = await pool.lease()
        cid = lease.container.container_id
        await lease.release()
        await _eventually(lambda: len(rt.create_specs) == 4)
        assert (cid, True) in rt.removed  # 归还即销毁
        assert len(rt.create_specs) == 4  # 后台补建 1 个
        assert pool.snapshot()["idle"] == 3

    async def test_execute_convenience(self, pool: SandboxPool) -> None:
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        rt.exec_fn = lambda cid, cmd, wd: ExecOutcome(0, "ok\n", "")
        result = await pool.execute("echo ok")
        assert result.exit_code == 0
        assert result.stdout == "ok\n"
        await _drain()
        assert pool.snapshot()["leases"] == 1

    async def test_start_is_idempotent_guarded(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=1, runtime=fake_runtime)
        await pool.start()
        await pool.close()
        with pytest.raises(SandboxError, match="池已关闭"):
            await pool.start()

    async def test_close_destroys_idle(self, pool: SandboxPool) -> None:
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        await pool.close()
        assert len(rt.removed) == 3
        with pytest.raises(SandboxError, match="池已关闭"):
            await pool.lease()


class TestBackpressure:
    async def test_exhausted_pool_queues_request(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=1, runtime=fake_runtime)
        await pool.start()
        first = await pool.lease()
        waiter = asyncio.create_task(pool.lease())
        await asyncio.sleep(0.05)
        assert not waiter.done()  # 在排队，没有失败
        await first.release()
        second = await waiter
        assert second.container.container_id != first.container.container_id

    async def test_max_queue_overflow_fails_fast(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=1, runtime=fake_runtime, max_queue=1)
        await pool.start()
        holder = await pool.lease()
        waiter1 = asyncio.create_task(pool.lease())
        await asyncio.sleep(0.05)
        waiter2 = asyncio.create_task(pool.lease())
        # 第二个等待者超过 max_queue=1，快速失败；第一个继续排队
        with pytest.raises(PoolExhaustedError):
            await waiter2
        # 先释放持有者，排队中的 waiter1 才能拿到补建的容器
        await holder.release()
        queued = await waiter1
        await queued.release()
        await _drain()

    async def test_twenty_concurrent_on_small_pool(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=5, runtime=fake_runtime, trace_sink=FakeTraceSink())
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        concurrent = 0
        max_concurrent = 0

        def exec_fn(cid: str, cmd: list[str], wd: str) -> ExecOutcome:
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            concurrent -= 1
            return ExecOutcome(0, "", "")

        rt.exec_fn = exec_fn
        results = await asyncio.gather(*(pool.execute("echo x", timeout_s=5.0) for _ in range(20)))
        assert len(results) == 20
        assert all(r.exit_code == 0 for r in results)
        assert max_concurrent <= 5
        await _drain()
        snapshot = pool.snapshot()
        assert snapshot["leases"] == 20
        assert snapshot["executions"] == 20


class TestHealthCheckAndRefill:
    async def test_killed_idle_container_evicted_on_lease(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=2, runtime=fake_runtime)
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        # 全部标记死亡（预热并发进行，ID 与池内顺序无对应），模拟外部 kill -9
        rt.set_alive("cid-0001", False)
        rt.set_alive("cid-0002", False)

        lease = await pool.lease()
        # 租借必须淘汰死容器并改取补建的新容器，绝不返回死容器
        assert lease.container.container_id in ("cid-0003", "cid-0004")
        assert ("cid-0001", True) in rt.removed
        assert ("cid-0002", True) in rt.removed
        await _eventually(lambda: len(rt.create_specs) == 4)  # 两个死容器都被补建

    async def test_refill_failure_only_shrinks_pool(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=1, runtime=fake_runtime)
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        lease = await pool.lease()

        original_create = rt.create

        def broken_create(spec: Any) -> str:
            raise SandboxError("daemon 挂了")

        rt.create = broken_create  # type: ignore[method-assign]
        await lease.release()
        await _drain()  # 补建失败被吞成日志，不向外抛
        assert pool.snapshot()["idle"] == 0
        rt.create = original_create  # type: ignore[method-assign]


class TestMetrics:
    async def test_reuse_counted_within_lease(self, pool: SandboxPool) -> None:
        await pool.start()
        lease = await pool.lease()
        await lease.execute("echo 1")
        await lease.execute("echo 2")
        assert pool.snapshot()["executions"] == 2
        assert pool.snapshot()["reuse_count"] == 1  # 第二次执行算复用
        await lease.release()
        await _drain()

    async def test_lease_wait_recorded(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=1, runtime=fake_runtime)
        await pool.start()
        first = await pool.lease()
        waiter = asyncio.create_task(pool.lease())
        await asyncio.sleep(0.05)
        await first.release()
        await waiter
        await _drain()
        snapshot = pool.snapshot()
        assert snapshot["lease_wait_total_ms"] >= 40
        assert snapshot["lease_wait_max_ms"] >= 40

    async def test_resource_peak_sampled(self, fake_runtime: FakeRuntime) -> None:
        pool = SandboxPool(size=1, runtime=fake_runtime)
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        rt.stats_results["cid-0001"] = {"memory_mb": 200.0, "cpu_percent": 80.0}
        await pool.execute("echo x")
        await _eventually(lambda: pool.snapshot()["peak_memory_mb"] == 200.0)
        snapshot = pool.snapshot()
        assert snapshot["peak_memory_mb"] == 200.0
        assert snapshot["peak_cpu_percent"] == 80.0


class TestTraceIntegration:
    async def test_lease_traced_via_sink(self, pool: SandboxPool) -> None:
        sink = FakeTraceSink()
        pool._trace_sink = sink  # type: ignore[assignment]
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]
        rt.exec_fn = lambda cid, cmd, wd: ExecOutcome(0, "", "")
        await pool.execute("echo x")
        await _drain()
        assert sink.created == ["sandbox_pool"]
        assert sink.updates == [("trace-1", {"tool_call_count": 1})]
        assert sink.completed == [("trace-1", "completed")]

    async def test_failed_execution_marks_error_status(self, pool: SandboxPool) -> None:
        sink = FakeTraceSink()
        pool._trace_sink = sink  # type: ignore[assignment]
        await pool.start()
        rt: FakeRuntime = pool._runtime  # type: ignore[assignment]

        def failing(cid: str, cmd: list[str], wd: str) -> ExecOutcome:
            raise SandboxError("exec 通道挂死")

        rt.exec_fn = failing
        with pytest.raises(SandboxError):
            await pool.execute("echo x")
        await _drain()
        assert sink.completed == [("trace-1", "error")]


class TestInvalidArgs:
    def test_rejects_zero_size(self, fake_runtime: FakeRuntime) -> None:
        with pytest.raises(ValueError):
            SandboxPool(size=0, runtime=fake_runtime)

    def test_rejects_negative_queue(self, fake_runtime: FakeRuntime) -> None:
        with pytest.raises(ValueError):
            SandboxPool(size=1, runtime=fake_runtime, max_queue=-1)
