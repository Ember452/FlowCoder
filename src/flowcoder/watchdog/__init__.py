"""仓库看门狗子包：信号 → Agent 判定 → 防骚扰门控 → 主动提醒（P5b）。

- signals.py：可插拔信号源（git 状态 / 测试结果恶化 / 文件变更）
- judge.py：Agent 结构化判定"是否值得主动提示"（解析失败保守沉默）
- gate.py：ProactiveGate 四层门控（去重 / 冷却 / 每日上限 / 多时间尺度衰减）
- store.py：门控状态持久化（重启不重发）
- gatekeeper.py：Watchdog 主循环与巡检账目
"""

from flowcoder.watchdog.gate import GateConfig, GateDecision, GateState, ProactiveGate
from flowcoder.watchdog.gatekeeper import Watchdog, WatchdogReport
from flowcoder.watchdog.judge import LLMJudge, Verdict, parse_verdict
from flowcoder.watchdog.signals import (
    FileChangeSource,
    GitStatusSource,
    Signal,
    SignalSource,
    TestResultsSource,
)
from flowcoder.watchdog.store import GateStateStore

__all__ = [
    "FileChangeSource",
    "GateConfig",
    "GateDecision",
    "GateState",
    "GateStateStore",
    "GitStatusSource",
    "LLMJudge",
    "ProactiveGate",
    "Signal",
    "SignalSource",
    "TestResultsSource",
    "Verdict",
    "Watchdog",
    "WatchdogReport",
    "parse_verdict",
]
