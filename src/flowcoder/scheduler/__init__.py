"""调度器子包：cron 驱动的自动化值守（P5a）。

- cron.py：5 字段 cron 自实现（解析 + next_after，零依赖）
- latency.py：滚动 P90 触发延迟 → 软实时预触发窗口
- store.py：任务定义/调度状态/运行记录 JSON 持久化（重启恢复）
- runner.py：Scheduler 主循环（防抖合并、失败重试、预触发）
- daemon_job.py：把任务提交为 daemon 会话的 Agent 回合（复用任务注册表）
"""

from flowcoder.scheduler.cron import CronError, CronExpr
from flowcoder.scheduler.daemon_job import DaemonJobExecutor
from flowcoder.scheduler.latency import LatencyTracker
from flowcoder.scheduler.runner import Scheduler
from flowcoder.scheduler.store import (
    JobDefinition,
    JobState,
    RunRecord,
    ScheduleState,
    ScheduleStore,
)

__all__ = [
    "CronError",
    "CronExpr",
    "DaemonJobExecutor",
    "JobDefinition",
    "JobState",
    "LatencyTracker",
    "RunRecord",
    "ScheduleState",
    "ScheduleStore",
    "Scheduler",
]
