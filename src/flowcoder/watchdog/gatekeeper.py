"""看门狗主循环（P5b）：信号 → 去重 → Agent 判定 → 防骚扰门控 → 送达。

与 scheduler 相同的可测轮询模型：`poll_once(now)` 纯推进，`run_forever()`
是守护包装。全部判定结果（送达/拦截及原因）经 `WatchdogReport` 返回并
可累加成"连续运行 1 天"的验收数据（触发次数 / 拦截次数 / 0 重复推送）。

编排顺序及理由：
  ① 信号源 poll（实现方保证不重复吐同 key）
  ② 去重查表（O(1)，在花 LLM token 之前拦下重复事件）
  ③ Agent 结构化判定（不值得 → 记录并跳过）
  ④ 防骚扰门控（冷却/每日上限/能量衰减——判定管"值不值得"，门控管"礼不礼貌"）
  ⑤ 送达 → record_delivery（去重表/冷却/每日计数/衰减历史落盘）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from flowcoder.watchdog.gate import GateDecision, ProactiveGate
from flowcoder.watchdog.judge import WorthinessJudge
from flowcoder.watchdog.signals import Signal, SignalSource
from flowcoder.watchdog.store import GateStateStore

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 60.0

Deliverer = Callable[[Signal, str], Awaitable[None]]


@dataclass
class WatchdogReport:
    """一轮巡检的账目（验收数据来源）。"""

    signals_seen: int = 0
    duplicates: int = 0
    judged_not_worthy: int = 0
    gate_blocked: int = 0
    delivered: int = 0
    block_reasons: dict[str, int] = field(default_factory=dict)

    def total_intercepted(self) -> int:
        return self.duplicates + self.judged_not_worthy + self.gate_blocked


@dataclass
class _Delivery:
    signal: Signal
    verdict_reason: str


class Watchdog:
    def __init__(
        self,
        sources: Iterable[SignalSource],
        *,
        judge: WorthinessJudge,
        gate: ProactiveGate,
        store: GateStateStore,
        deliverer: Deliverer,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._sources = list(sources)
        self._judge = judge
        self._gate = gate
        self._store = store
        self._deliverer = deliverer
        self._now = now_fn

    async def poll_once(self, now: float | None = None) -> WatchdogReport:
        now = now if now is not None else self._now()
        report = WatchdogReport()
        for source in self._sources:
            try:
                signals: list[Signal] = await source.poll()
            except Exception as e:  # 信号源故障不拖垮看门狗
                logger.warning("信号源 %s 巡检失败：%s", type(source).__name__, e)
                continue
            for signal in signals:
                await self._process(signal, now, report)
        if report.delivered:
            self._store.save(self._gate.state, config=self._gate.config)
        return report

    async def _process(self, signal: Signal, now: float, report: WatchdogReport) -> None:
        report.signals_seen += 1
        if self._gate.is_duplicate(signal.delivery_key):
            report.duplicates += 1
            self._tally(report, "duplicate")
            return

        verdict = await self._judge.judge(signal)
        if not verdict.worth_prompting:
            report.judged_not_worthy += 1
            logger.info("信号判定不值得提示（%s）：%s", signal.kind, verdict.reason)
            return

        decision: GateDecision = self._gate.decide(signal.delivery_key, now=now)
        if not decision.allowed:
            report.gate_blocked += 1
            self._tally(report, decision.reason)
            logger.info("信号被门控拦截（%s）：%s", decision.reason, signal.summary)
            return

        await self._deliverer(signal, verdict.reason)
        self._gate.record_delivery(signal.delivery_key, now=now)
        report.delivered += 1

    @staticmethod
    def _tally(report: WatchdogReport, reason: str) -> None:
        report.block_reasons[reason] = report.block_reasons.get(reason, 0) + 1

    async def run_forever(self, *, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S) -> None:
        logger.info("看门狗启动：%d 个信号源", len(self._sources))
        while True:
            await self.poll_once(self._now())
            await asyncio.sleep(poll_interval_s)


async def _noop_deliverer(signal: Signal, reason: str) -> None:  # pragma: no cover
    logger.info("送达提醒：%s（%s）", signal.summary, reason)
