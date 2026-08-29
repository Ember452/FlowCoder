"""调度器 CLI（P5a 演示/验收入口）。

    python -m flowcoder.scheduler --cron "* * * * *" --prompt "巡检" \
        --state ~/.flowcoder/scheduler.json

独立进程运行调度循环（真实时钟）。executor 默认为日志演示——生产接入
经 DaemonJobExecutor(daemon_server) 把任务提交为 daemon 会话的 Agent 回合
（见 docs/architecture/scheduler.md）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from flowcoder.scheduler.latency import LatencyTracker
from flowcoder.scheduler.runner import Scheduler
from flowcoder.scheduler.store import ScheduleStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m flowcoder.scheduler")
    parser.add_argument("--cron", default="* * * * *", help="任务 cron 表达式（默认每分钟）")
    parser.add_argument("--prompt", default="定时巡检", help="任务提交内容")
    parser.add_argument("--name", default="demo", help="任务名")
    parser.add_argument(
        "--state",
        default=str(Path.home() / ".flowcoder" / "scheduler.json"),
        help="持久化文件路径（重启恢复）",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    return parser


async def _demo_executor(job) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] 触发任务 {job.name}：{job.prompt!r}")


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    scheduler = Scheduler(
        ScheduleStore(Path(args.state)),
        _demo_executor,
        latency=LatencyTracker(),
    )
    scheduler.add_job(args.name, args.cron, args.prompt)
    print(f"调度器已启动：{args.name} ({args.cron})，状态文件 {args.state}，Ctrl+C 退出")
    await scheduler.run_forever()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n调度器已停止")
        return 0
    except Exception as e:
        print(f"调度器异常退出：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
