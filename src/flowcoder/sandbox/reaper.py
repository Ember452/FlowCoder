"""泄漏回收：心跳对账，孤儿容器强杀（P1b）。

租借时在池台账登记 container_id 与归属 task_id；reaper 周期对账，
归属任务已结束但容器未归还的判为孤儿，强制销毁并触发池补建。
任务是否存活由调用方注入的回调判断（daemon 任务注册表等），
sandbox 不感知上层任务模型。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from flowcoder.sandbox.pool import LeaseRecord, SandboxPool
from flowcoder.sandbox.runtime import SandboxError

logger = logging.getLogger(__name__)


class LeaseReaper:
    """周期对账租借台账，回收孤儿容器。"""

    def __init__(
        self,
        pool: SandboxPool,
        *,
        is_task_running: Callable[[str], bool],
        interval_s: float = 30.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s 必须为正数")
        self._pool = pool
        self._is_task_running = is_task_running
        self._interval_s = interval_s

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            await self.reconcile_once()

    async def reconcile_once(self) -> list[str]:
        """对账一轮，返回本轮回收的容器 ID。"""
        reaped: list[str] = []
        for record in list(self.pool_active()):
            if record.task_id is None:
                continue  # 无归属信息的租借不判孤儿，避免误杀
            if self._is_task_running(record.task_id):
                continue
            await self._reap(record)
            reaped.append(record.container_id)
        return reaped

    async def _reap(self, record: LeaseRecord) -> None:
        logger.warning("回收孤儿容器（任务已结束未归还）：%s", record.container_id)
        self.pool_active_remove(record.container_id)
        runtime = self._pool._ensure_runtime()
        try:
            await asyncio.to_thread(runtime.remove, record.container_id, force=True)
        except SandboxError as exc:
            logger.error("孤儿容器销毁失败：%s %s", record.container_id, exc)
        self._pool._spawn_replacement()

    def pool_active(self) -> list[LeaseRecord]:
        """台账快照（池的活跃租借记录）。"""
        return list(self._pool._active.values())

    def pool_active_remove(self, container_id: str) -> None:
        self._pool._active.pop(container_id, None)
