"""防骚扰门控测试：去重、冷却、每日上限、能量衰减（P5b）。"""

from __future__ import annotations

from flowcoder.watchdog.gate import GateConfig, GateState, ProactiveGate

DAY = 86400.0


class ManualClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _gate(clock: ManualClock, **config) -> ProactiveGate:
    return ProactiveGate(GateState(), config=GateConfig(**config), now_fn=clock)


class TestDedup:
    def test_same_key_never_resent(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=0, daily_limit=100, energy_cap=1e9)
        assert gate.decide("k1", now=clock()).allowed
        gate.record_delivery("k1", now=clock())
        clock.now += DAY  # 冷却/每日/能量全部重置也不行
        decision = gate.decide("k1", now=clock())
        assert not decision.allowed
        assert decision.reason == "duplicate"

    def test_different_key_passes_dedup(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=0, daily_limit=100, energy_cap=1e9)
        gate.record_delivery("k1", now=clock())
        assert not gate.is_duplicate("k2")


class TestCooldown:
    def test_cooldown_intercepts(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=1800.0, daily_limit=100, energy_cap=1e9)
        gate.record_delivery("k1", now=1000.0)
        decision = gate.decide("k2", now=1000.0 + 1799)
        assert not decision.allowed and decision.reason == "cooldown"
        decision = gate.decide("k2", now=1000.0 + 1800)
        assert decision.allowed


class TestDailyLimit:
    def test_daily_cap(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=0, daily_limit=3, energy_cap=1e9)
        for i in range(3):
            gate.record_delivery(f"k{i}", now=clock())
        decision = gate.decide("k9", now=clock())
        assert not decision.allowed and decision.reason == "daily-limit"
        # 次日计数归零，放行
        decision = gate.decide("k9", now=clock() + DAY)
        assert decision.allowed

    def test_daily_count_is_per_calendar_day(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=0, daily_limit=1, energy_cap=1e9)
        gate.record_delivery("k1", now=1000.0)
        # 同一天稍晚：仍受限
        assert gate.decide("k2", now=1000.0 + 3600).reason == "daily-limit"


class TestEnergyDecay:
    def test_rapid_deliveries_hit_energy_cap(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=0, daily_limit=100, energy_cap=3.0)
        # 连续送达：能量 Σα·exp(-Δt/τ) 快速超过 3.0（两个尺度 α 和 1.5）
        gate.record_delivery("k1", now=1000.0)
        gate.record_delivery("k2", now=1001.0)  # 能量 ≈ 1+0.5+1+0.5 = 3
        gate.record_delivery("k3", now=1002.0)
        decision = gate.decide("k4", now=1002.5)
        assert not decision.allowed and decision.reason == "energy"

    def test_decay_reopens_over_time(self) -> None:
        clock = ManualClock(1000.0)
        gate = _gate(clock, cooldown_s=0, daily_limit=100, energy_cap=3.0)
        gate.record_delivery("k1", now=1000.0)
        gate.record_delivery("k2", now=1001.0)
        gate.record_delivery("k3", now=1002.0)
        # 6 小时后：1h 尺度衰减殆尽，24h 尺度仍贡献但低于容量
        decision = gate.decide("k4", now=1000.0 + 6 * 3600)
        assert decision.allowed


class TestStatePersistence:
    def test_gate_state_survives_restart(self, tmp_path) -> None:
        from flowcoder.watchdog.store import GateStateStore

        store = GateStateStore(tmp_path / "gate.json")
        clock = ManualClock(1000.0)
        gate = ProactiveGate(store.load(), config=GateConfig(), now_fn=clock)
        gate.record_delivery("seen-once", now=1000.0)
        store.save(gate.state)

        # "重启"：新实例从磁盘恢复
        gate2 = ProactiveGate(store.load(), config=GateConfig(), now_fn=clock)
        assert gate2.is_duplicate("seen-once")
        assert not gate2.decide("seen-once", now=clock()).allowed  # 重启后仍不重发
        assert gate2.state.last_delivery_at == 1000.0

    def test_corrupt_store_starts_clean(self, tmp_path) -> None:
        from flowcoder.watchdog.store import GateStateStore

        path = tmp_path / "gate.json"
        path.write_text("{broken", encoding="utf-8")
        store = GateStateStore(path)
        assert store.load().delivered_keys == set()
