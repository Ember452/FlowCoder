"""调度器主循环：到期判定、防抖合并、失败重试、软实时预触发（P5a）。

设计为可测的轮询模型：`poll_once(now)` 是纯推进函数（给定时钟，判定到期、
执行、记账），`run_forever()` 只是把 sleep + poll_once 包成守护循环。
测试注入合成时钟即可驱动"10 分钟每分钟任务"的完整时间线，不必真实等待。

核心语义（ADR 详见 docs/specs P5a）：
- 防抖合并（coalesce）：停机/拥塞期间错过的多个窗口合并为**一次**执行，
  错过窗口数记入 RunRecord.coalesced——补跑 N 次旧任务通常无意义且危险。
- 失败重试：每次执行失败按指数退避重试，上限 max_retries；耗尽记
  retry_exhausted，连续失败计数供运维观察。
- 软实时预触发：唤醒时刻 = next_run - LatencyTracker.pre_trigger_window()，
  用实测延迟的滚动 P90 把实际执行点拉回时间窗口内。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable

from flowcoder.scheduler.cron import CronExpr
from flowcoder.scheduler.latency import LatencyTracker
from flowcoder.scheduler.store import (
    JobDefinition,
    JobState,
    RunRecord,
    ScheduleState,
    ScheduleStore,
)

logger = logging.getLogger(__name__)

Executor = Callable[[JobDefinition], Awaitable[None]]

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_S = 1.0
DEFAULT_TICK_S = 0.2


class Scheduler:
    """cron 驱动的任务调度器。add_job 后 run_forever()；测试可逐次 poll_once。"""

    def __init__(
        self,
        store: ScheduleStore,
        executor: Executor,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_s: float = DEFAULT_RETRY_BASE_S,
        latency: LatencyTracker | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._executor = executor
        self._max_retries = max_retries
        self._retry_base_s = retry_base_s
        self._latency = latency or LatencyTracker()
        self._now = now_fn
        self.state: ScheduleState = store.load()

    # ------------------------------------------------------------------ 管理

    def add_job(self, name: str, cron: str, prompt: str, *, enabled: bool = True) -> None:
        """注册或更新任务；cron 变更后 next_run 重算，未变更则沿用原调度点。"""
        CronExpr.parse(cron)  # 先校验再入库
        old_def = self.state.jobs.get(name)
        old_state = self.state.states.get(name)
        cron_unchanged = old_def is not None and old_def.cron == cron
        next_run = old_state.next_run if cron_unchanged and old_state is not None else None
        self.state.jobs[name] = JobDefinition(name=name, cron=cron, prompt=prompt, enabled=enabled)
        self.state.states[name] = JobState(next_run=next_run)
        self._ensure_next_run(name)
        self._store.save(self.state)

    def remove_job(self, name: str) -> None:
        self.state.jobs.pop(name, None)
        self.state.states.pop(name, None)
        self._store.save(self.state)

    def jobs(self) -> dict[str, JobDefinition]:
        return dict(self.state.jobs)

    # ------------------------------------------------------------------ 循环

    async def run_forever(self) -> None:
        """守护循环：睡到下一个预触发点或 tick 粒度，逐次 poll_once。"""
        logger.info("调度器启动：%d 个任务", len(self.state.jobs))
        while True:
            wake_in = self._next_wake_in()
            await asyncio.sleep(max(wake_in, DEFAULT_TICK_S / 4))
            await self.poll_once(self._now())

    def _next_wake_in(self) -> float:
        """距最近一个（已减去预触发提前量的）调度点的等待秒数。"""
        now = self._now()
        deadlines = [
            st.next_run - self._latency.pre_trigger_window()
            for st in self.state.states.values()
            if st.next_run is not None
        ]
        if not deadlines:
            return DEFAULT_TICK_S
        return max(0.0, min(deadlines) - now)

    # ------------------------------------------------------------------ 推进

    async def poll_once(self, now: float) -> list[RunRecord]:
        """推进一轮：处理全部到期任务（含防抖、重试），返回本次产生的运行记录。

        到期判定用预触发窗口：now >= next_run - P90 即触发——提前量补偿
        从触发到任务实际生效的启动延迟（软实时，见 latency.py）。
        """
        produced: list[RunRecord] = []
        for name, job in list(self.state.jobs.items()):
            if not job.enabled:
                continue
            job_state = self.state.states.setdefault(name, JobState())
            self._ensure_next_run(name, now=now)
            next_run = job_state.next_run
            if next_run is None:
                continue
            due_at = next_run - self._latency.pre_trigger_window()
            if now < due_at:
                continue
            record = await self._fire_job(job, job_state, now)
            produced.append(record)
            self._store.append_run(self.state, record)
            # 防抖推进：next_run 跳过所有已错过的窗口（错过数记入 coalesced）；
            # 早于计划的预触发不影响推进基准（从 next_run 起算）
            new_next = self._advance_past(job, max(next_run, now))
            record.coalesced = self._coalesced_count(job, next_run, new_next)
            job_state.next_run = new_next
            self._store.save(self.state)
        return produced

    async def _fire_job(self, job: JobDefinition, job_state: JobState, now: float) -> RunRecord:
        """执行一次任务并按指数退避重试，返回运行记录（含成败与尝试次数）。"""
        scheduled_for = job_state.next_run or now
        # 实测触发延迟喂给预触发器；只采样"触发路径"的小延迟——
        # 停机/拥塞导致的巨量错过不是 loop 延迟，采样会污染 P90 并把
        # 窗口顶到上限（每分钟任务会因此连续触发）
        delay = now - scheduled_for
        if delay <= self._latency.max_pre_trigger_s:
            self._latency.record(delay)
        record = RunRecord(
            job=job.name,
            scheduled_for=scheduled_for,
            started_at=now,
        )
        for attempt in range(1, self._max_retries + 1):
            record.attempts = attempt
            try:
                await self._executor(job)
                record.status = "success"
                job_state.consecutive_failures = 0
                break
            except Exception as e:  # 任务失败不拖垮调度循环
                record.error = f"{type(e).__name__}: {e}"
                logger.warning("任务 %s 第 %d 次执行失败：%s", job.name, attempt, e)
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_base_s * (2 ** (attempt - 1)))
        else:
            record.status = "retry_exhausted"
            job_state.consecutive_failures += 1
        record.finished_at = self._now()
        return record

    # ------------------------------------------------------------------ 内部

    def _ensure_next_run(self, name: str, *, now: float | None = None) -> None:
        """next_run 缺失时由 cron 重算（首次注册 / 重启恢复兜底）。"""
        state = self.state.states.get(name)
        job = self.state.jobs.get(name)
        if state is None or job is None or state.next_run is not None:
            return
        base = now if now is not None else self._now()
        next_dt = CronExpr.parse(job.cron).next_after(_epoch_to_datetime(base))
        state.next_run = next_dt.timestamp()

    def _advance_past(self, job: JobDefinition, moment: float) -> float:
        """计算严格晚于 moment 的下一个 cron 触发时刻（epoch 秒）。"""
        return CronExpr.parse(job.cron).next_after(_epoch_to_datetime(moment)).timestamp()

    def _coalesced_count(self, job: JobDefinition, missed_from: float, new_next: float) -> int:
        """(missed_from, new_next) 区间内被合并掉的窗口数。"""
        cron = CronExpr.parse(job.cron)
        return cron.next_fire_count_between(
            _epoch_to_datetime(missed_from), _epoch_to_datetime(new_next)
        )


def _epoch_to_datetime(epoch: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch)
