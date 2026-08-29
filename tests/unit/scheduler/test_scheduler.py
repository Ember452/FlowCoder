"""调度器主循环测试：防抖合并、失败重试上限、预触发、重启恢复（P5a）。

时间全部走注入的合成时钟——"每分钟任务连跑 10 分钟"的验收在测试里
以加速时间线驱动，逐分钟 poll_once，断言 10 次运行、无重复、记录完整。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowcoder.scheduler.latency import LatencyTracker
from flowcoder.scheduler.runner import Scheduler
from flowcoder.scheduler.store import ScheduleStore


class FakeClock:
    """合成时钟：手动推进。"""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingExecutor:
    """记录触发的 fake executor，可注入失败。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[str] = []
        self._fail_times = fail_times

    async def __call__(self, job) -> None:
        self.calls.append(job.name)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("注入失败")


MINUTE = 60.0


@pytest.fixture
def clock() -> FakeClock:
    # 锚定某个 10:00:30，避免边界巧合
    return FakeClock(start=1756460400.0 + 30)


def _make(tmp_path: Path, clock: FakeClock, executor, **kwargs) -> Scheduler:
    return Scheduler(
        ScheduleStore(tmp_path / "schedules.json"),
        executor,
        latency=LatencyTracker(max_pre_trigger_s=60.0),
        now_fn=clock,
        **kwargs,
    )


class TestBasicScheduling:
    async def test_every_minute_run_ten_simulated_minutes(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        """验收模拟：每分钟任务连续跑 10 分钟（加速时间线），
        运行记录完整、无重复触发。"""
        executor = RecordingExecutor()
        sched = _make(tmp_path, clock, executor)
        sched.add_job("per-minute", "* * * * *", "hello")

        # 推进 10 分钟：逐段 poll_once
        for _ in range(10):
            clock.advance(MINUTE)
            await sched.poll_once(clock())

        assert executor.calls == ["per-minute"] * 10
        runs = sched.state.runs
        assert len(runs) == 10
        assert all(r.status == "success" for r in runs)
        assert len({r.scheduled_for for r in runs}) == 10  # 无重复触发窗口
        assert all(r.coalesced == 0 for r in runs)

    async def test_not_due_before_schedule(self, tmp_path: Path, clock: FakeClock) -> None:
        executor = RecordingExecutor()
        sched = _make(tmp_path, clock, executor)
        sched.add_job("hourly", "0 * * * *", "x")
        clock.advance(MINUTE)
        await sched.poll_once(clock())
        assert executor.calls == []

    async def test_disabled_job_skipped(self, tmp_path: Path, clock: FakeClock) -> None:
        executor = RecordingExecutor()
        sched = _make(tmp_path, clock, executor)
        sched.add_job("off", "* * * * *", "x", enabled=False)
        clock.advance(MINUTE)
        await sched.poll_once(clock())
        assert executor.calls == []


class TestCoalesce:
    async def test_missed_windows_merge_into_one_run(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        """停机 7 分钟（错过 7 个窗口）→ 只跑一次，coalesced 记 6。"""
        executor = RecordingExecutor()
        sched = _make(tmp_path, clock, executor)
        sched.add_job("m", "* * * * *", "x")
        clock.advance(7 * MINUTE)
        await sched.poll_once(clock())

        assert executor.calls == ["m"]  # 合并为一次
        runs = sched.state.runs
        assert len(runs) == 1
        assert runs[0].coalesced == 6

    async def test_next_run_advances_past_now(self, tmp_path: Path, clock: FakeClock) -> None:
        """防抖后下一次触发在未来，不会连环补跑。"""
        executor = RecordingExecutor()
        sched = _make(tmp_path, clock, executor)
        sched.add_job("m", "* * * * *", "x")
        clock.advance(7 * MINUTE)
        await sched.poll_once(clock())
        clock.advance(0.0)
        await sched.poll_once(clock())
        assert executor.calls == ["m"]  # 立即再 poll 不会重复触发


class TestRetry:
    async def test_retry_with_backoff_then_success(self, tmp_path: Path, clock: FakeClock) -> None:
        executor = RecordingExecutor(fail_times=2)  # 前两次失败
        sched = _make(tmp_path, clock, executor, max_retries=3, retry_base_s=0.0)
        sched.add_job("m", "* * * * *", "x")
        clock.advance(MINUTE)
        await sched.poll_once(clock())

        assert executor.calls == ["m"] * 3
        runs = sched.state.runs
        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].attempts == 3

    async def test_retry_exhaustion_recorded(self, tmp_path: Path, clock: FakeClock) -> None:
        executor = RecordingExecutor(fail_times=99)
        sched = _make(tmp_path, clock, executor, max_retries=2, retry_base_s=0.0)
        sched.add_job("m", "* * * * *", "x")
        clock.advance(MINUTE)
        await sched.poll_once(clock())

        runs = sched.state.runs
        assert runs[0].status == "retry_exhausted"
        assert runs[0].attempts == 2
        assert "RuntimeError" in runs[0].error
        assert sched.state.states["m"].consecutive_failures == 1

    async def test_success_resets_failure_counter(self, tmp_path: Path, clock: FakeClock) -> None:
        executor = RecordingExecutor(fail_times=2)
        sched = _make(tmp_path, clock, executor, max_retries=2, retry_base_s=0.0)
        sched.add_job("m", "* * * * *", "x")
        clock.advance(MINUTE)
        await sched.poll_once(clock())  # retry_exhausted（2 次全败）
        assert sched.state.states["m"].consecutive_failures == 1
        clock.advance(MINUTE)
        await sched.poll_once(clock())  # 本次成功
        assert sched.state.states["m"].consecutive_failures == 0


class TestPreTrigger:
    def test_latency_p90_and_window_clamp(self) -> None:
        tracker = LatencyTracker(max_pre_trigger_s=60.0)
        for _ in range(10):
            tracker.record(3.0)
        assert tracker.p90() == 3.0
        assert tracker.pre_trigger_window() == 3.0
        # 封顶：超大延迟不产生离谱的预触发窗口
        big = LatencyTracker(max_pre_trigger_s=5.0)
        for _ in range(5):
            big.record(120.0)
        assert big.pre_trigger_window() == 5.0
        assert LatencyTracker().p90() == 0.0

    async def test_pre_trigger_window_wakes_early(self, tmp_path: Path, clock: FakeClock) -> None:
        """P90 延迟 3s → 唤醒点提前 3s：poll 在 T-3 已触发，执行点贴近 T。"""
        tracker = LatencyTracker(max_pre_trigger_s=60.0)
        for _ in range(10):
            tracker.record(3.0)
        assert tracker.p90() == 3.0
        assert tracker.pre_trigger_window() == 3.0

        executor = RecordingExecutor()
        sched = Scheduler(
            ScheduleStore(tmp_path / "schedules.json"),
            executor,
            latency=tracker,
            now_fn=clock,
        )
        sched.add_job("m", "* * * * *", "x")
        scheduled = sched.state.states["m"].next_run
        # 提前 2.9s（窗口 3s 内）poll：应当已触发
        clock.now = scheduled - 2.9
        await sched.poll_once(clock())
        assert executor.calls == ["m"]
        # 实测延迟被记录为约 2.9s，喂回 tracker
        assert len(tracker) == 11


class TestPersistence:
    async def test_restart_recovers_state(self, tmp_path: Path, clock: FakeClock) -> None:
        """重启恢复：新 Scheduler 从磁盘读回任务与调度点，不重复触发已跑窗口。"""
        executor = RecordingExecutor()
        store = ScheduleStore(tmp_path / "schedules.json")
        sched = Scheduler(store, executor, now_fn=clock, latency=LatencyTracker())
        sched.add_job("m", "* * * * *", "x")
        clock.advance(MINUTE)
        await sched.poll_once(clock())
        first_scheduled = sched.state.runs[0].scheduled_for

        # "重启"：新实例加载同一存储
        clock.advance(MINUTE)
        restarted = Scheduler(store, executor, now_fn=clock, latency=LatencyTracker())
        assert "m" in restarted.jobs()
        await restarted.poll_once(clock())
        # 只触发新窗口，不重放已执行窗口
        assert executor.calls == ["m", "m"]
        assert restarted.state.runs[-1].scheduled_for > first_scheduled
        assert len({r.scheduled_for for r in restarted.state.runs}) == 2

    async def test_corrupt_store_starts_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        path.write_text("{broken json", encoding="utf-8")
        sched = Scheduler(ScheduleStore(path), RecordingExecutor(), now_fn=lambda: 0.0)
        assert sched.jobs() == {}

    async def test_run_records_capped(self, tmp_path: Path, clock: FakeClock) -> None:
        store = ScheduleStore(tmp_path / "s.json", run_limit=5)
        sched = Scheduler(store, RecordingExecutor(), now_fn=clock, latency=LatencyTracker())
        sched.add_job("m", "* * * * *", "x")
        for _ in range(8):
            clock.advance(MINUTE)
            await sched.poll_once(clock())
        assert len(sched.state.runs) == 5
        raw = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
        assert len(raw["runs"]) == 5


class TestJobManagement:
    async def test_cron_change_recomputes_next_run(self, tmp_path: Path, clock: FakeClock) -> None:
        sched = _make(tmp_path, clock, RecordingExecutor())
        sched.add_job("m", "* * * * *", "x")
        old_next = sched.state.states["m"].next_run
        sched.add_job("m", "0 * * * *", "x")  # 改为每小时
        new_next = sched.state.states["m"].next_run
        assert new_next != old_next
        assert new_next > clock()

    async def test_remove_job(self, tmp_path: Path, clock: FakeClock) -> None:
        sched = _make(tmp_path, clock, RecordingExecutor())
        sched.add_job("m", "* * * * *", "x")
        sched.remove_job("m")
        assert sched.jobs() == {}
        clock.advance(MINUTE)
        await sched.poll_once(clock())  # 不应报错也不应触发
