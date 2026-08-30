"""调度器持久化：任务定义、调度状态与运行记录（P5a）。

单 JSON 文件 + 原子写（tmp + rename）：
- 任务定义：name / cron / prompt / enabled——重启后从磁盘恢复；
- 调度状态：next_run（epoch 秒）/ 连续失败数——next_run 缺失时由 cron
  重新计算（重启恢复的兜底路径）；
- 运行记录：滚动上限内的逐次执行台账（状态/尝试数/错误），验收要求
  "连续跑 10 分钟运行记录完整"的数据来源。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from flowcoder.core.atomic import write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_RUN_LIMIT = 200


@dataclass
class JobDefinition:
    name: str
    cron: str
    prompt: str
    enabled: bool = True


@dataclass
class JobState:
    next_run: float | None = None  # epoch 秒；None = 待 cron 重算
    consecutive_failures: int = 0


@dataclass
class RunRecord:
    job: str
    scheduled_for: float  # 计划触发时刻（epoch 秒）
    started_at: float
    finished_at: float | None = None
    status: str = "running"  # running / success / failed / retry_exhausted
    attempts: int = 1
    coalesced: int = 0  # 防抖合并掉的错过窗口数
    error: str = ""


@dataclass
class ScheduleState:
    jobs: dict[str, JobDefinition] = field(default_factory=dict)
    states: dict[str, JobState] = field(default_factory=dict)
    runs: list[RunRecord] = field(default_factory=list)


class ScheduleStore:
    """JSON 文件持久化。load() 在启动时调用（重启恢复），save() 幂等原子写。"""

    def __init__(self, path: Path | str, *, run_limit: int = DEFAULT_RUN_LIMIT) -> None:
        self._path = Path(path)
        self._run_limit = run_limit

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ScheduleState:
        if not self._path.exists():
            return ScheduleState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # 配置损坏不应让守护进程起不来：记日志、从空状态开始
            logger.error("调度器状态文件损坏，忽略并从空状态开始：%s", e)
            return ScheduleState()
        state = ScheduleState()
        for name, job in raw.get("jobs", {}).items():
            state.jobs[name] = JobDefinition(
                name=name,
                cron=job["cron"],
                prompt=job.get("prompt", ""),
                enabled=job.get("enabled", True),
            )
        for name, st in raw.get("states", {}).items():
            state.states[name] = JobState(
                next_run=st.get("next_run"),
                consecutive_failures=st.get("consecutive_failures", 0),
            )
        for run in raw.get("runs", [])[-self._run_limit :]:
            state.runs.append(RunRecord(**run))
        return state

    def save(self, state: ScheduleState) -> None:
        payload = {
            "jobs": {name: asdict(job) for name, job in state.jobs.items()},
            "states": {name: asdict(st) for name, st in state.states.items()},
            "runs": [asdict(r) for r in state.runs[-self._run_limit :]],
        }
        write_json_atomic(self._path, payload)

    def append_run(self, state: ScheduleState, record: RunRecord) -> None:
        state.runs.append(record)
        if len(state.runs) > self._run_limit:
            del state.runs[: len(state.runs) - self._run_limit]
