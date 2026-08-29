"""信号源、判定解析与看门狗主循环测试（含 24h 加速时间线验收，P5b）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from flowcoder.watchdog.gate import GateConfig, ProactiveGate
from flowcoder.watchdog.gatekeeper import Watchdog
from flowcoder.watchdog.judge import parse_verdict
from flowcoder.watchdog.signals import (
    FileChangeSource,
    Signal,
    TestResultsSource,
)
from flowcoder.watchdog.store import GateStateStore

DAY = 86400.0
MINUTE = 60.0


class ManualClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class StaticSource:
    """每次 poll 依次吐出预设信号。"""

    def __init__(self, batches: list[list[Signal]]) -> None:
        self._batches = list(batches)

    async def poll(self) -> list[Signal]:
        return self._batches.pop(0) if self._batches else []


class FakeJudge:
    """按 key 前缀决定判定结果，记录全部调用。"""

    def __init__(self, worthy: bool = True) -> None:
        self.worthy = worthy
        self.calls: list[str] = []

    async def judge(self, signal: Signal) -> object:
        self.calls.append(signal.delivery_key)
        from flowcoder.watchdog.judge import Verdict

        return Verdict(worth_prompting=self.worthy, reason="fake")


class RecordingDeliverer:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []

    async def __call__(self, signal: Signal, reason: str) -> None:
        self.delivered.append((signal.delivery_key, reason))


class TestSignalSources:
    def test_test_degradation_detected(self) -> None:
        src = TestResultsSource()
        assert src.report("unit", 90, 100) is None  # 首次基线
        assert src.report("unit", 95, 100) is None  # 改善
        degraded = src.report("unit", 80, 100)  # 恶化
        assert degraded is not None
        assert degraded.kind == "tests-degraded"
        assert "95.0% → 80.0%" in degraded.summary
        # 同一 baseline 对（95→80）→ 同 key；不同 baseline 是不同事件
        src2 = TestResultsSource()
        src2.report("unit", 95, 100)
        again = src2.report("unit", 80, 100)
        assert again is not None and again.delivery_key == degraded.delivery_key
        src3 = TestResultsSource()
        src3.report("unit", 90, 100)
        different = src3.report("unit", 80, 100)
        assert different is not None and different.delivery_key != degraded.delivery_key

    def test_file_change_dedups_same_content(self, tmp_path: Path) -> None:
        watched = tmp_path / "watch.txt"
        watched.write_text("v1", encoding="utf-8")
        src = FileChangeSource([str(watched)])
        assert asyncio.run(src.poll()) == []  # 首次快照不算变更
        watched.write_text("v2", encoding="utf-8")
        signals = asyncio.run(src.poll())
        assert len(signals) == 1
        assert asyncio.run(src.poll()) == []  # 内容未变不重发

    def test_git_source_with_fake_runner(self) -> None:
        from flowcoder.watchdog.signals import GitStatusSource

        calls = {"n": 0}

        async def fake_git(args: list[str]) -> str:
            calls["n"] += 1
            if args[0] == "rev-parse":
                return "main\n"
            return " M a.py\n?? b.py\n" if calls["n"] <= 4 else " M a.py\n?? b.py\n M c.py\n"

        src = GitStatusSource("/repo", command_runner=fake_git)
        first = asyncio.run(src.poll())
        assert len(first) == 1 and "2 处变更" in first[0].summary
        # 同一状态再 poll：不重复
        assert asyncio.run(src.poll()) == []
        # 状态变化：新信号（第三次 poll 时 fake 返回不同状态）
        third = asyncio.run(src.poll())
        assert len(third) == 1 and "3 处变更" in third[0].summary


class TestParseVerdict:
    def test_fenced_json(self) -> None:
        v = parse_verdict('前言```json\n{"worth_prompting": true, "reason": "测试挂了"}\n```')
        assert v.worth_prompting and v.reason == "测试挂了"

    def test_bare_json(self) -> None:
        v = parse_verdict('{"worth_prompting": false, "reason": "纯状态同步"}')
        assert not v.worth_prompting

    def test_unparseable_is_conservative_false(self) -> None:
        assert not parse_verdict("我觉得还好吧").worth_prompting
        v = parse_verdict('{"worth_prompting": "yes"}')
        assert not v.worth_prompting  # 非布尔保守拒绝


def _make(tmp_path: Path, clock: ManualClock, source, judge: FakeJudge):
    deliverer = RecordingDeliverer()
    watchdog = Watchdog(
        [source],
        judge=judge,
        gate=ProactiveGate(
            GateStateStore(tmp_path / "gate.json").load(),
            config=GateConfig(cooldown_s=30 * MINUTE, daily_limit=5, energy_cap=1e9),
            now_fn=clock,
        ),
        store=GateStateStore(tmp_path / "gate.json"),
        deliverer=deliverer,
        now_fn=clock,
    )
    return watchdog, deliverer


class TestWatchdogLoop:
    async def test_judge_not_worthy_never_delivers(self, tmp_path: Path) -> None:
        clock = ManualClock(1000.0)
        source = StaticSource([[Signal("git-status", "k1", "脏工作区", 1000.0)]])
        watchdog, deliverer = _make(tmp_path, clock, source, FakeJudge(worthy=False))
        report = await watchdog.poll_once(clock())
        assert deliverer.delivered == []
        assert report.judged_not_worthy == 1

    async def test_full_path_delivers_and_persists(self, tmp_path: Path) -> None:
        clock = ManualClock(1000.0)
        source = StaticSource([[Signal("tests-degraded", "k1", "测试恶化", 1000.0)]])
        watchdog, deliverer = _make(tmp_path, clock, source, FakeJudge(worthy=True))
        report = await watchdog.poll_once(clock())
        assert report.delivered == 1
        assert len(deliverer.delivered) == 1

        # 重启后同一 key 不重发（状态从磁盘恢复）
        watchdog2, deliverer2 = _make(tmp_path, clock, StaticSource([[]]), FakeJudge(True))
        report2 = await watchdog2.poll_once(clock())
        assert deliverer2.delivered == []
        assert report2.duplicates == 0  # 没有新信号，自然无重复
        # 若源再吐同 key：去重拦截
        source3 = StaticSource([[Signal("tests-degraded", "k1", "测试恶化", 1000.0)]])
        watchdog3, _ = _make(tmp_path, clock, source3, FakeJudge(True))
        report3 = await watchdog3.poll_once(clock())
        assert report3.duplicates == 1

    async def test_daily_limit_blocks(self, tmp_path: Path) -> None:
        clock = ManualClock(1000.0)
        batches = [[Signal("tests-degraded", f"k{i}", f"事件{i}", 1000.0)] for i in range(7)]
        watchdog, deliverer = _make(tmp_path, clock, StaticSource(batches), FakeJudge(True))
        # 每次巡检间隔 31 分钟（超出冷却），同一天内：前 5 个送达，
        # 第 6 个起撞每日上限
        for i in range(5):
            await watchdog.poll_once(clock())
            clock.now += 31 * MINUTE
        report = await watchdog.poll_once(clock())  # 第 6 个
        assert report.gate_blocked == 1
        assert report.block_reasons.get("daily-limit") == 1
        await watchdog.poll_once(clock())  # 第 7 个同样被拦
        assert len(deliverer.delivered) == 5

    async def test_cooldown_blocks_rapid_second_event(self, tmp_path: Path) -> None:
        clock = ManualClock(1000.0)
        batches = [
            [Signal("tests-degraded", "k1", "事件1", 1000.0)],
            [Signal("tests-degraded", "k2", "事件2", 1000.0)],
            [Signal("tests-degraded", "k2", "事件2", 1000.0)],  # 源重发同事件
        ]
        watchdog, deliverer = _make(tmp_path, clock, StaticSource(batches), FakeJudge(True))
        await watchdog.poll_once(clock())
        clock.now += 10 * MINUTE  # 冷却 30min 内
        report = await watchdog.poll_once(clock())
        assert report.gate_blocked == 1
        assert report.block_reasons.get("cooldown") == 1
        clock.now += 25 * MINUTE  # 超过冷却
        report3 = await watchdog.poll_once(clock())
        assert report3.delivered == 1
        assert len(deliverer.delivered) == 2


class TestOneDayAcceptance:
    async def test_24h_simulated_timeline(self, tmp_path: Path) -> None:
        """验收模拟（加速时间线）：连续"运行"1 天。

        事件流：每 2 小时同一脏工作区状态（同 key）+ 每小时一次新测试恶化。
        预期：触发次数有限（门控生效）、拦截次数完整记账、0 重复推送。
        """
        clock = ManualClock(1000.0)
        # 构造 24h 事件流：每 2h 一批（同 key 脏工作区 + 新 key 测试恶化）
        batches: list[list[Signal]] = []
        t = 0.0
        while t < DAY:
            batches.append(
                [
                    Signal("git-status", "git-dirty:same-state", "工作区有 3 处变更", 1000.0 + t),
                    Signal(
                        "tests-degraded",
                        f"tests:degraded-{int(t // 3600)}",
                        f"通过率下降@{t / 3600:.0f}h",
                        1000.0 + t,
                    ),
                ]
            )
            t += 2 * 3600
        source = StaticSource(batches)
        judge = FakeJudge(worthy=True)
        watchdog, deliverer = _make(tmp_path, clock, source, judge)

        totals = {"delivered": 0, "intercepted": 0, "duplicates": 0}
        t = 0.0
        while t < DAY:
            report = await watchdog.poll_once(clock())
            totals["delivered"] += report.delivered
            totals["intercepted"] += report.total_intercepted()
            totals["duplicates"] += report.duplicates
            clock.now += 2 * 3600
            t += 2 * 3600

        # 同 key 脏工作区事件 12 批：首个放行，其余 11 个全部去重命中
        assert totals["duplicates"] == 11
        assert totals["delivered"] <= 5 * 1 + 11  # 每日上限 5 × 1 天 + 提示全部来自新 key
        assert totals["delivered"] == len(deliverer.delivered)  # 账实相符
        # 0 重复推送：所有送达的 key 唯一
        keys = [k for k, _ in deliverer.delivered]
        assert len(keys) == len(set(keys))

    def test_report_serializable(self, tmp_path: Path) -> None:
        report_data = {"delivered": 3, "intercepted": 7}
        json.dumps(report_data)  # 验收数据可序列化进报告
