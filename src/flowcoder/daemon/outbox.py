"""Outbox 事件投递核心（P5c）：偏移量、结果未知账本、保留期清理。

选型：**JSONL 追加文件**而非 SQLite。理由（ADR D1）：daemon 事件流的
持久化层（session store 的 events.jsonl）本身就是逐事件 fsync 的追加
日志，天然满足 Outbox 的"先落盘再投递"；补投只需要 seq 单调 + 按行
过滤，SQLite 的事务能力在这个单写多读、无随机更新的场景是过剩抽象。
保留期清理以"重写文件"实现（保留行数 << 总行数时重写成本低）。

关键语义：
- **seq**：会话内单调递增的事件序号，emit 时盖到事件上并持久化；
  重连用 `?since=<seq>` 从断点补投，seq 连续性即"不丢不重"的验收依据。
- **结果未知的投递不重放**（保守投递语义，借鉴 flow-agent）：交互类事件
  （PermissionRequest / AskUserRequest）绑定连接生命周期的 future——
  已推送给某客户端但未获 ack 时，投递结果未知；重连补投时**跳过**
  这类事件（跳过一次即"已知跳过"），未决交互由 pending-prompt 机制
  重新出账，而不是重放一条 future 已死的事件。
- **保留期清理不误删未投递**：只删除"seq ≤ 客户端已 ack 且年龄超过
  保留期"的事件；无 ts 的历史事件视为未知年龄，一律保留。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from flowcoder.core.atomic import write_text_atomic

logger = logging.getLogger(__name__)

#: 绑定连接生命周期（future 不可跨连接）的事件类型：结果未知时补投无意义
NO_REPLAY_TYPES = {"PermissionRequest", "AskUserRequest"}


def event_seq(event: dict) -> int | None:
    """读取事件的持久化序号（emit 时盖章）；无则 None。"""
    seq = event.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


class OutboxLedger:
    """进程内账本：哪些交互事件已推送但 ack 未知（按会话）。"""

    def __init__(self) -> None:
        self._pushed_unknown: dict[str, set[int]] = {}

    def mark_pushed(self, sid: str, seq: int) -> None:
        self._pushed_unknown.setdefault(sid, set()).add(seq)

    def take_unknown(self, sid: str) -> set[int]:
        """取走（并清空）某会话的结果未知集合——重连补投时逐个跳过后，
        投递结果即"已知跳过"，不再累积。"""
        return self._pushed_unknown.pop(sid, set())

    def ack(self, sid: str, upto: int) -> None:
        known = self._pushed_unknown.get(sid)
        if known:
            known -= {seq for seq in known if seq <= upto}
            if not known:
                self._pushed_unknown.pop(sid, None)


def should_replay(event: dict, unknown_seqs: set[int]) -> bool:
    """补投判定：结果未知的交互事件不重放，其余（含普通事件的已推未 ack）
    一律重放——客户端对渲染类事件幂等。"""
    seq = event_seq(event)
    if seq is not None and seq in unknown_seqs and event.get("type") in NO_REPLAY_TYPES:
        return False
    return True


def cleanup_outbox_file(
    path: Path,
    *,
    now: float,
    retention_s: float,
    acked_seq: int,
) -> tuple[int, int]:
    """按保留期重写 outbox 文件，返回 (保留数, 删除数)。

    删除规则：seq <= acked_seq（客户端确认已收到）**且**年龄超过 retention_s。
    未 ack 的事件无论多旧都保留——不误删未投递。无 seq/ts 的历史行一律保留。
    """
    if not path.exists():
        return (0, 0)
    kept_lines: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(line)  # 无法解析的行不碰
            continue
        seq = event_seq(event)
        ts = event.get("ts")
        if (
            seq is not None
            and seq <= acked_seq
            and isinstance(ts, (int, float))
            and now - ts > retention_s
        ):
            dropped += 1
            continue
        kept_lines.append(line)
    # 无删除时不重写文件——避免每次清理都触发一次磁盘 IO（保留期默认 72h）
    if dropped == 0:
        return (len(kept_lines), 0)
    write_text_atomic(path, "\n".join(kept_lines) + ("\n" if kept_lines else ""))
    return (len(kept_lines), dropped)


async def outbox_cleanup_loop(server: Any, *, interval_s: float = 3600.0) -> None:
    """守护清理循环：按保留期周期性清理已投递的过期事件。"""
    while True:
        await asyncio.sleep(interval_s)
        try:
            dropped = server.cleanup_outbox()
            if dropped:
                logger.info("Outbox 保留期清理：%s", dropped)
        except Exception:
            logger.exception("Outbox 清理失败")


def build_outbox_lifespan(server: Any, *, interval_s: float = 3600.0) -> Any:
    """Starlette lifespan：app 生命周期内挂载清理守护任务。"""

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        task = asyncio.create_task(outbox_cleanup_loop(server, interval_s=interval_s))
        yield
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    return lifespan
