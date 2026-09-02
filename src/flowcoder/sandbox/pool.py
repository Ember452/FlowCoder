"""容器池：预热租借、用完销毁重建、排队背压（P1b）。

- 启动时预热 N 个容器（默认 10），租借 O(1)（deque 左弹）。
- 归还即销毁，后台补建同规格容器，保证每次执行环境全新、无状态残留。
- 池耗尽时请求在 asyncio.Condition 上排队（背压而非拒绝）；等待者超过
  max_queue 时抛 PoolExhaustedError（快速失败，防请求无限堆积）。
- 空闲容器被外部 kill：租借前 is_alive 体检，死容器销毁并补建，重试取下一个。
- TraceSink：鸭子类型 Protocol，结构上兼容 agents.trace.TraceManager（create/
  update/complete 三个方法），避免 sandbox 反向依赖上层模块。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from flowcoder.sandbox.container import (
    ExecutionResult,
    SandboxConfig,
    SandboxContainer,
)
from flowcoder.sandbox.metrics import SandboxMetrics
from flowcoder.sandbox.runtime import ContainerRuntime, DockerRuntime, SandboxError

logger = logging.getLogger(__name__)

#: 池创建的容器统一打标，reaper/启动清理据此识别归属
SANDBOX_LABEL = "flowcoder.sandbox"


class PoolExhaustedError(SandboxError):
    """池耗尽且等待队列超过 max_queue 上限。"""


class TraceSink(Protocol):
    """TraceManager 的结构化子集：池只借用调用树追踪，不引入依赖。"""

    def create(
        self, agent_type: str, parent_id: str | None = None, trace_id: str | None = None
    ) -> Any: ...

    def update(self, agent_id: str, **kwargs: int | str) -> None: ...

    def complete(self, agent_id: str, status: str = "completed") -> None: ...


@dataclass
class LeaseRecord:
    """租借台账：reaper 对账的依据。"""

    container_id: str
    task_id: str | None
    leased_at: float


class Lease:
    """一次租借：通过 execute 在容器上执行命令，release 归还（销毁重建）。"""

    def __init__(self, pool: SandboxPool, container: SandboxContainer, task_id: str | None) -> None:
        self.container = container
        self.task_id = task_id
        self._pool = pool
        self._released = False
        self._trace_agent_id: str | None = None
        self._exec_count = 0
        self._status = "completed"

    async def execute(
        self,
        command: str | list[str],
        *,
        files: Mapping[str, bytes | str] | None = None,
        timeout_s: float = 30.0,
        kill_grace_s: float = 2.0,
    ) -> ExecutionResult:
        """在底层容器上执行命令；据此更新状态并回写池的执行/资源统计。"""
        result = await self.container.execute(
            command, files=files, timeout_s=timeout_s, kill_grace_s=kill_grace_s
        )
        self._exec_count += 1
        if result.timed_out:
            self._status = "timeout"
        elif result.exit_code not in (0, None):
            self._status = "failed"
        self._pool._record_execution(self, result)
        return result

    def mark_failed(self) -> None:
        """执行抛异常时由调用方标记，release 时写入 trace 状态。"""
        self._status = "error"

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._pool._release(self)


class SandboxPool:
    """预热式容器池。start() 后可 lease/execute；close() 清空全部空闲容器。"""

    def __init__(
        self,
        *,
        size: int = 10,
        config: SandboxConfig | None = None,
        runtime: ContainerRuntime | None = None,
        max_queue: int | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        if size <= 0:
            raise ValueError("size 必须为正数")
        if max_queue is not None and max_queue < 0:
            raise ValueError("max_queue 不能为负数")
        self._size = size
        self._base_config = config or SandboxConfig()
        self._labeled_config = replace(
            self._base_config, labels={SANDBOX_LABEL: f"pool-{uuid.uuid4().hex[:8]}"}
        )
        self._runtime = runtime
        self._max_queue = max_queue
        self._trace_sink = trace_sink
        self._idle: deque[SandboxContainer] = deque()
        self._cond: asyncio.Condition | None = None
        self._waiting = 0
        self._active: dict[str, LeaseRecord] = {}
        self._exec_counts: dict[str, int] = {}
        self.metrics = SandboxMetrics()
        self._closed = False
        self._bg_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """清理上次运行遗留的沙箱容器，再预热 N 个。"""
        if self._closed:
            raise SandboxError("池已关闭")
        runtime = self._ensure_runtime()
        stale = await asyncio.to_thread(runtime.list_by_label, SANDBOX_LABEL)
        for cid in stale:
            logger.warning("清理上次运行遗留的沙箱容器：%s", cid)
            try:
                await asyncio.to_thread(runtime.remove, cid, force=True)
            except SandboxError as exc:
                logger.error("遗留容器清理失败（继续预热）：%s %s", cid, exc)
        await asyncio.gather(*(self._create_and_stock() for _ in range(self._size)))

    async def lease(self, *, task_id: str | None = None, trace_id: str | None = None) -> Lease:
        """租借一个健康容器；池空则排队（背压），超过 max_queue 快速失败。"""
        if self._closed:
            raise SandboxError("池已关闭")
        runtime = self._ensure_runtime()
        started = time.perf_counter()
        container = await self._acquire(started)
        # 空闲期被外部 kill 的容器：体检淘汰、后台补建，重新排队取健康的
        while not await asyncio.to_thread(runtime.is_alive, container.container_id):
            logger.warning("租借体检发现死容器，销毁并补建：%s", container.container_id)
            await asyncio.to_thread(runtime.remove, container.container_id, force=True)
            self._spawn_replacement()
            container = await self._acquire(started)

        cid = container.container_id
        assert cid is not None
        self._active[cid] = LeaseRecord(
            container_id=cid, task_id=task_id, leased_at=time.monotonic()
        )
        lease = Lease(self, container, task_id)
        if self._trace_sink is not None:
            lease._trace_agent_id = self._trace_sink.create(
                "sandbox_pool", trace_id=trace_id
            ).agent_id
        return lease

    async def execute(
        self,
        command: str | list[str],
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        files: Mapping[str, bytes | str] | None = None,
        timeout_s: float = 30.0,
        kill_grace_s: float = 2.0,
    ) -> ExecutionResult:
        """便捷入口：租借 → 执行 → 归还。"""
        lease = await self.lease(task_id=task_id, trace_id=trace_id)
        try:
            return await lease.execute(
                command, files=files, timeout_s=timeout_s, kill_grace_s=kill_grace_s
            )
        except SandboxError:
            lease.mark_failed()
            raise
        finally:
            await lease.release()

    async def close(self) -> None:
        """销毁全部空闲容器并拒绝后续请求；在租借中的容器由调用方自行 release。"""
        if self._closed:
            return
        self._closed = True
        while self._idle:
            container = self._idle.popleft()
            try:
                await container.close()
            except SandboxError as exc:
                logger.error("空闲容器销毁失败：%s %s", container.container_id, exc)
        cond = self._cond
        if cond is not None:
            async with cond:
                cond.notify_all()

    def snapshot(self) -> dict[str, int | float]:
        return self.metrics.snapshot(
            pool_size=self._size,
            idle=len(self._idle),
            active=len(self._active),
        )

    # ------------------------------------------------------------------ 内部

    async def _release(self, lease: Lease) -> None:
        """归还租借：注销台账、记账、销毁容器并按需后台补建。"""
        cid = lease.container.container_id
        if cid is not None:
            self._active.pop(cid, None)
            self._exec_counts.pop(cid, None)
        if self._trace_sink is not None and lease._trace_agent_id is not None:
            self._trace_sink.complete(lease._trace_agent_id, lease._status)
        self.metrics.record_lease_released()
        try:
            await lease.container.close()
        except SandboxError as exc:
            logger.error("归还容器销毁失败：%s %s", cid, exc)
        if not self._closed:
            self._spawn_replacement()

    def _spawn_replacement(self) -> None:
        """后台补建容器，保持池水位；失败只记日志，池允许暂时缩水。"""
        task = asyncio.create_task(self._create_and_stock())
        task.add_done_callback(self._log_refill_failure)

    @staticmethod
    def _log_refill_failure(task: asyncio.Task[None]) -> None:
        """补建任务的完成回调：吞掉取消/无异常场景，仅记录真实失败。"""
        if not task.cancelled() and task.exception() is not None:
            logger.error("容器补建失败，池水位暂时下降：%s", task.exception())

    async def _create_and_stock(self) -> SandboxContainer:
        """建一个容器并入池：预热阶段直接入 idle，后续入 idle 并唤醒一个等待者。"""
        container = SandboxContainer(config=self._labeled_config, runtime=self._runtime)
        await container.start()
        cond = self._cond
        if cond is None:  # start() 预热阶段，lease 尚未开始
            self._idle.append(container)
            return container
        async with cond:
            self._idle.append(container)
            cond.notify(1)
        return container

    async def _acquire(self, started: float) -> SandboxContainer:
        """取一个空闲容器；池空则排队等待（背压），超上限快速失败。"""
        cond = self._cond
        if cond is None:
            cond = self._cond = asyncio.Condition()
        async with cond:
            self._waiting += 1
            try:
                while True:
                    if self._closed:
                        raise SandboxError("池已关闭")
                    if self._idle:
                        container = self._idle.popleft()
                        break
                    if self._max_queue is not None and self._waiting > self._max_queue:
                        raise PoolExhaustedError(f"池耗尽且等待队列超过上限 {self._max_queue}")
                    await cond.wait()
            finally:
                self._waiting -= 1
        # 等待时长在拿到容器后落账，才反映真实排队时间
        self.metrics.record_lease_wait(int((time.perf_counter() - started) * 1000))
        return container

    def _record_execution(self, lease: Lease, result: ExecutionResult) -> None:
        """落账一次执行：复用判定、耗时指标、trace 工具数、资源采样。"""
        cid = lease.container.container_id
        if cid is None:
            return
        reused = self._exec_counts.get(cid, 0) > 0
        self._exec_counts[cid] = self._exec_counts.get(cid, 0) + 1
        self.metrics.record_execution(result.duration_ms, reused=reused)
        if self._trace_sink is not None and lease._trace_agent_id is not None:
            self._trace_sink.update(lease._trace_agent_id, tool_call_count=lease._exec_count)
        self._sample_resources(cid)

    def _sample_resources(self, container_id: str) -> None:
        """best-effort 资源采样；容器刚退出等场景取不到就跳过。"""
        runtime = self._runtime
        if runtime is None:
            return

        async def _sample() -> None:
            try:
                usage = await asyncio.to_thread(runtime.stats, container_id)
            except SandboxError:
                return
            self.metrics.record_resource(usage["memory_mb"], usage["cpu_percent"])

        task = asyncio.create_task(_sample())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _ensure_runtime(self) -> ContainerRuntime:
        if self._runtime is None:
            self._runtime = DockerRuntime.from_env()
        return self._runtime
