"""防骚扰门控（P5b）。

思想借鉴 flow-agent 的 ProactiveGate（冷却间隔 / delivery_key 去重 / 每日
上限三件套），代码按本项目风格重写，并叠加第四层：**多时间尺度能量衰减**
——E(t) = Σ_past Σ_i α_i·exp(-Δt_i/τ_i)，每次历史送达按多个时间尺度贡献
衰减中的"提醒能量"；当前能量超过容量则软性拦截（比每日上限更平滑的
频率调节，公式自实现）。

判定顺序（gatekeeper 编排，本模块只做单层判定）：
  去重（同 key 永不重发）→ 冷却（硬性最小间隔）→ 每日上限（硬性）
  → 能量衰减（软性频率调节）→ 放行
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str  # duplicate / cooldown / daily-limit / energy / ok


@dataclass
class GateConfig:
    cooldown_s: float = 1800.0  # 冷却间隔（硬性最小提醒间隔）
    daily_limit: int = 10  # 每日提醒上限
    #: 多时间尺度衰减参数：(alpha, tau_s) 对；能量容量 cap
    decay_scales: tuple[tuple[float, float], ...] = (
        (1.0, 3600.0),  # 1 小时尺度
        (0.5, 86400.0),  # 24 小时尺度
    )
    energy_cap: float = 3.0
    history_limit: int = 200  # 参与衰减计算的历史送达条数上限


@dataclass
class GateState:
    """门控状态（由 store 持久化，重启不重发）。"""

    delivered_keys: set[str] = field(default_factory=set)  # 永不重发
    delivery_times: list[float] = field(default_factory=list)  # 历史送达时刻（衰减输入）
    daily_counts: dict[str, int] = field(default_factory=dict)  # "YYYY-MM-DD" → 次数
    last_delivery_at: float | None = None


def _date_key(now: float) -> str:
    """把时刻规约成 UTC 日界字符串，作为每日计数的键。"""
    # UTC 日界：确定性、可测试，且避免极端时钟值在 Windows 触发
    # fromtimestamp 的本地时区越界
    import datetime as dt

    return dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).strftime("%Y-%m-%d")


class ProactiveGate:
    """四层防骚扰判定。decide 只读，record_delivery 在实际送达后调用。"""

    def __init__(
        self,
        state: GateState,
        *,
        config: GateConfig | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.state = state
        self.config = config or GateConfig()
        self._now = now_fn or time.time

    # ---------------------------------------------------------------- 判定

    def is_duplicate(self, delivery_key: str) -> bool:
        return delivery_key in self.state.delivered_keys

    def decide(self, delivery_key: str, *, now: float | None = None) -> GateDecision:
        """四层判定：去重 → 冷却 → 每日上限 → 能量衰减；任一不满足即拦截。"""
        now = now if now is not None else self._now()
        st = self.state
        cfg = self.config
        if self.is_duplicate(delivery_key):
            return GateDecision(False, "duplicate")
        if st.last_delivery_at is not None and now - st.last_delivery_at < cfg.cooldown_s:
            return GateDecision(False, "cooldown")
        if st.daily_counts.get(_date_key(now), 0) >= cfg.daily_limit:
            return GateDecision(False, "daily-limit")
        if self.energy(now) >= cfg.energy_cap:
            return GateDecision(False, "energy")
        return GateDecision(True, "ok")

    def energy(self, now: float) -> float:
        """E(t) = Σ 历史 × Σ_scale α·exp(-Δt/τ)，多时间尺度叠加。"""
        total = 0.0
        for t in self.state.delivery_times[-self.config.history_limit :]:
            dt = max(0.0, now - t)
            for alpha, tau in self.config.decay_scales:
                total += alpha * math.exp(-dt / tau)
        return total

    # ---------------------------------------------------------------- 记账

    def record_delivery(self, delivery_key: str, *, now: float | None = None) -> None:
        """实际送达后记账（幂等：重复 key 不重复计数）。"""
        now = now if now is not None else self._now()
        st = self.state
        if delivery_key in st.delivered_keys:
            return
        st.delivered_keys.add(delivery_key)
        st.delivery_times.append(now)
        if len(st.delivery_times) > self.config.history_limit:
            del st.delivery_times[: len(st.delivery_times) - self.config.history_limit]
        date = _date_key(now)
        st.daily_counts[date] = st.daily_counts.get(date, 0) + 1
        # 顺带清理 7 天前的每日计数，防无界增长
        cutoff = _date_key(max(0.0, now - 7 * 86400))
        st.daily_counts = {d: c for d, c in st.daily_counts.items() if d >= cutoff}
        st.last_delivery_at = now
