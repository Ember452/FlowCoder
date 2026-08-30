"""daemon 后台服务装配（P5.5）：调度器 / 看门狗 / 预算的统一接线点。

装配原则（ADR 见 docs/specs/2026-08-29-wiring-p55-adr.md）：
- **默认全关**：配置缺省时不创建任何后台任务，行为与从前完全一致；
- 集中在本模块，app.py（god file 禁令）与 server_state 不新增装配逻辑；
- watchdog/scheduler 的守护任务经 lifespan 起、停（cancel）。

预算闸不走后台任务：它在 `agent/factory.create_agent_from_config` 中
按 config.budget 注入 Agent（daemon/CLI/eval 全覆盖；TUI 自建 Agent
的接线随 R3 app.py 清算一并处理）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from flowcoder.config import AppConfig

logger = logging.getLogger(__name__)


async def build_budget_for_agent(config: AppConfig):
    """按配置构造 Budget（供 agent 工厂注入）；未配置返回 None。"""
    from flowcoder.agent.budget import Budget

    if config is None or config.budget is None:
        return None
    b = config.budget
    return Budget(
        max_total_tokens=b.max_total_tokens,
        max_turns=b.max_turns,
        max_seconds=b.max_seconds,
        max_cost_usd=b.max_cost_usd,
        input_price_per_1m=b.input_price_per_1m,
        output_price_per_1m=b.output_price_per_1m,
    )


def _build_scheduler(server, config: AppConfig):
    from flowcoder.scheduler import DaemonJobExecutor, Scheduler
    from flowcoder.scheduler.store import ScheduleStore

    state_file = config.scheduler.state_file or str(
        Path(server.work_dir) / ".flowcoder" / "scheduler.json"
    )
    scheduler = Scheduler(
        ScheduleStore(state_file),
        DaemonJobExecutor(server),
    )
    for job in config.scheduler.jobs:
        scheduler.add_job(job.name, job.cron, job.prompt)
    logger.info("后台调度器已装配：%d 个任务（state=%s）", len(config.scheduler.jobs), state_file)
    return scheduler.run_forever()


def _build_watchdog(server, config: AppConfig):
    from flowcoder.client import create_client
    from flowcoder.watchdog import (
        FileChangeSource,
        GateConfig,
        GitStatusSource,
        LLMJudge,
        ProactiveGate,
        Watchdog,
    )
    from flowcoder.watchdog.store import GateStateStore
    from flowcoder.daemon.watchdog_job import WatchdogDeliverer

    sources = []
    if config.watchdog.watch_git:
        sources.append(GitStatusSource(server.work_dir))
    if config.watchdog.paths:
        sources.append(FileChangeSource(config.watchdog.paths))
    if not sources:
        logger.warning("看门狗已启用但未配置任何信号源，跳过装配")
        return None

    gate_state = GateStateStore(Path(server.work_dir) / ".flowcoder" / "watchdog.json").load()
    watchdog = Watchdog(
        sources,
        judge=LLMJudge(create_client(config.providers[0])),
        gate=ProactiveGate(
            gate_state,
            config=GateConfig(
                cooldown_s=config.watchdog.cooldown_s,
                daily_limit=config.watchdog.daily_limit,
                energy_cap=config.watchdog.energy_cap,
            ),
        ),
        store=GateStateStore(Path(server.work_dir) / ".flowcoder" / "watchdog.json"),
        deliverer=WatchdogDeliverer(server),
    )
    logger.info("后台看门狗已装配：%d 个信号源", len(sources))
    return watchdog.run_forever(poll_interval_s=config.watchdog.poll_interval_s)


def build_background_lifespan(
    server, config: AppConfig | None, outbox_retention_s: float | None = None
):
    """Starlette lifespan：按配置起停调度器/看门狗守护任务 + Outbox 清理。"""

    @contextlib.asynccontextmanager
    async def lifespan(_app) -> AsyncIterator[None]:
        tasks: list[asyncio.Task] = []
        if config is not None and config.scheduler.enabled and config.scheduler.jobs:
            tasks.append(asyncio.create_task(_build_scheduler(server, config)))
        if config is not None and config.watchdog.enabled:
            coroutine = _build_watchdog(server, config)
            if coroutine is not None:
                tasks.append(asyncio.create_task(coroutine))
        if outbox_retention_s is not None:
            tasks.append(asyncio.create_task(_outbox_cleanup_loop(server)))

        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return lifespan


async def _outbox_cleanup_loop(server, *, interval_s: float = 3600.0) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            dropped = server.cleanup_outbox()
            if dropped:
                logger.info("Outbox 保留期清理：%s", dropped)
        except Exception:
            logger.exception("Outbox 清理失败")
