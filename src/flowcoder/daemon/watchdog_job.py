"""看门狗送达适配：提醒落地为 daemon 会话里的 Agent 回合（P5.5）。

与 scheduler/daemon_job.py 同构——watchdog 包保持 daemon 无关，
daemon 侧的适配集中在本模块。
"""

from __future__ import annotations

import logging
from typing import Any

from flowcoder.watchdog.signals import Signal

logger = logging.getLogger(__name__)


class WatchdogDeliverer:
    """把提醒提交为专用 daemon 会话的 Agent 回合（复用任务注册表）。"""

    def __init__(self, server: Any, work_dir: str | None = None) -> None:
        self._server = server
        self._work_dir = work_dir
        self._sid: str | None = None

    async def _ensure_session(self) -> str:
        if self._sid is None:
            self._sid = await self._server.init_session(work_dir=self._work_dir)
            logger.info("看门狗 daemon 会话就绪：%s", self._sid)
        return self._sid

    async def __call__(self, signal: Signal, reason: str) -> None:
        sid = await self._ensure_session()
        prompt = (
            f"[看门狗提醒] {signal.summary}\n判定理由：{reason}\n"
            "请基于以上信息判断是否需要进一步处理，并简要汇报。"
        )
        try:
            task_id = await self._server.start_task(sid, prompt)
        except Exception as e:
            # 会话可能已失效（重载/崩溃），重建后重试一次——避免提醒静默丢失
            logger.warning("看门狗提醒提交失败，重建会话重试：%s", e)
            self._sid = None
            sid = await self._ensure_session()
            task_id = await self._server.start_task(sid, prompt)
        logger.info("看门狗提醒已送达：session=%s task=%s", sid, task_id)
