"""调度器与 daemon 的集成：到期任务复用 daemon 任务注册表与执行路径（P5a）。

DaemonJobExecutor 持有一个 DaemonServer：首次触发时 init_session() 建一个
调度器专用会话（缓存 sid），每次触发 start_task(sid, prompt) 把 Agent 回合
提交进 daemon 的 ActiveTaskRegistry / AgentTaskRunner——与交互路径完全同源。

语义边界（ADR D4）：调度器对"任务"的职责是**按时触发 + 提交成功**；
Agent 回合本身的结果（token/工具/错误）由 daemon 会话台账（events.jsonl）
追踪，不回流到调度器的 RunRecord——两者是触发层与执行层的分工，
避免调度循环被长任务阻塞。
"""

from __future__ import annotations

import logging
from typing import Any

from flowcoder.scheduler.store import JobDefinition

logger = logging.getLogger(__name__)


class DaemonJobExecutor:
    """把 JobDefinition 的 prompt 提交为 daemon 会话里的 Agent 回合。"""

    def __init__(self, server: Any, work_dir: str | None = None) -> None:
        self._server = server
        self._work_dir = work_dir
        self._sid: str | None = None

    async def _ensure_session(self) -> str:
        if self._sid is None:
            self._sid = await self._server.init_session(work_dir=self._work_dir)
            logger.info("调度器 daemon 会话就绪：%s", self._sid)
        return self._sid

    async def __call__(self, job: JobDefinition) -> None:
        sid = await self._ensure_session()
        try:
            task_id = await self._server.start_task(sid, job.prompt)
        except Exception as e:
            # 会话可能已失效（重启清理等）：重建会话再试一次
            logger.warning("调度任务提交失败，重建 daemon 会话重试：%s", e)
            self._sid = None
            sid = await self._ensure_session()
            task_id = await self._server.start_task(sid, job.prompt)
        logger.info("调度任务 %s 已提交：session=%s task=%s", job.name, sid, task_id)

    @property
    def session_id(self) -> str | None:
        return self._sid
