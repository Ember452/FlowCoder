"""DaemonJobExecutor 与 daemon 集成测试（fake server，复用任务注册表的语义验证）。"""

from __future__ import annotations

from pathlib import Path


from flowcoder.scheduler.daemon_job import DaemonJobExecutor
from flowcoder.scheduler.runner import Scheduler
from flowcoder.scheduler.store import ScheduleStore


class FakeDaemonServer:
    """记录 init_session / start_task 调用的 fake daemon。"""

    def __init__(self, *, fail_first_submit: bool = False) -> None:
        self.init_calls: list[str | None] = []
        self.submitted: list[tuple[str, str]] = []
        self._fail_first_submit = fail_first_submit
        self._first_submit_done = False
        self._next_sid = 0

    async def init_session(self, work_dir: str | None = None) -> str:
        self.init_calls.append(work_dir)
        self._next_sid += 1
        return f"sid-{self._next_sid}"

    async def start_task(self, sid: str, prompt: str) -> str:
        if self._fail_first_submit and not self._first_submit_done:
            self._first_submit_done = True
            raise RuntimeError("session not found")
        self.submitted.append((sid, prompt))
        return f"task-{len(self.submitted)}"


class ImmediateClock:
    def __init__(self) -> None:
        self.now = 1756460400.0

    def __call__(self) -> float:
        return self.now


async def _submit_one(tmp_path: Path, server: FakeDaemonServer) -> Scheduler:
    clock = ImmediateClock()
    sched = Scheduler(
        ScheduleStore(tmp_path / "s.json"),
        DaemonJobExecutor(server, work_dir=str(tmp_path)),
        now_fn=clock,
    )
    sched.add_job("cron-job", "* * * * *", "每日站会纪要")
    clock.now += 60
    await sched.poll_once(clock.now)
    return sched


class TestDaemonJobExecutor:
    async def test_reuses_session_across_jobs(self, tmp_path: Path) -> None:
        server = FakeDaemonServer()
        executor = DaemonJobExecutor(server, work_dir="/tmp/wd")
        job = next(iter(_fake_jobs()))
        await executor(job)
        await executor(job)
        assert server.init_calls == ["/tmp/wd"]  # 只建一次会话
        assert len(server.submitted) == 2
        assert server.submitted[0][0] == server.submitted[1][0]

    async def test_session_rebuilt_on_failure(self, tmp_path: Path) -> None:
        server = FakeDaemonServer(fail_first_submit=True)
        executor = DaemonJobExecutor(server)
        job = next(iter(_fake_jobs()))
        await executor(job)  # 首次提交失败 → 重建会话 → 成功
        assert len(server.init_calls) == 2
        assert len(server.submitted) == 1

    async def test_scheduler_submission_recorded(self, tmp_path: Path) -> None:
        server = FakeDaemonServer()
        sched = await _submit_one(tmp_path, server)
        assert len(server.submitted) == 1
        sid, prompt = server.submitted[0]
        assert sid.startswith("sid-")
        assert prompt == "每日站会纪要"
        assert sched.state.runs[0].status == "success"


def _fake_jobs():
    from flowcoder.scheduler.store import JobDefinition

    return [JobDefinition(name="cron-job", cron="* * * * *", prompt="每日站会纪要")]
