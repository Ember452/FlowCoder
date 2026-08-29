"""触发延迟追踪与滚动 P90 软实时预触发（P5a）。

思想借鉴 akashic 的 LatencyTracker（代码自实现）：调度器"计划在 T 时刻
触发"但实际唤醒/执行启动总有延迟（事件循环拥塞、前序任务未结束等）。
记录实测延迟的滚动窗口，用 P90 作为**预提前量**——下一次在 T - P90 时刻
唤醒，使实际执行点尽量落回 T 附近，避免错过时间窗口（如"每天 09:00 出
报告"错过 09:05 就失去意义）。

P90 自实现：样本排序取 ceil(0.9*n)-1 位（n=0 时返回 0）。
"""

from __future__ import annotations

import math
from collections import deque


class LatencyTracker:
    """滚动窗口的触发延迟统计。"""

    def __init__(self, window: int = 64, *, max_pre_trigger_s: float = 60.0) -> None:
        if window <= 0:
            raise ValueError("window 必须为正数")
        if max_pre_trigger_s < 0:
            raise ValueError("max_pre_trigger_s 不能为负")
        self._delays: deque[float] = deque(maxlen=window)
        self._max_pre_trigger_s = max_pre_trigger_s

    def record(self, delay_s: float) -> None:
        """记录一次实测延迟（实际触发时刻 - 计划触发时刻，可为负=提前）。

        负值（预触发早于计划）按 0 计——预触发本身就是补偿，不应反向学习。
        """
        self._delays.append(max(0.0, delay_s))

    @property
    def max_pre_trigger_s(self) -> float:
        return self._max_pre_trigger_s

    def percentile(self, p: float) -> float:
        """p 分位延迟（秒）。p∈(0,1]；无样本返回 0。"""
        if not 0 < p <= 1:
            raise ValueError("p 必须在 (0,1]")
        if not self._delays:
            return 0.0
        ordered = sorted(self._delays)
        index = min(max(0, math.ceil(p * len(ordered)) - 1), len(ordered) - 1)
        return ordered[index]

    def p90(self) -> float:
        return self.percentile(0.9)

    def pre_trigger_window(self, *, min_s: float = 0.0) -> float:
        """下次触发的提前唤醒量：clamp(P90, min_s, max_pre_trigger_s)。"""
        return min(max(self.p90(), min_s), self._max_pre_trigger_s)

    def __len__(self) -> int:
        return len(self._delays)
