# 看门狗（watchdog/）

`src/flowcoder/watchdog/` 是主动式 Agent（P5b）：监听仓库信号，Agent 判断
"是否值得主动提示"，经四层防骚扰门控后送达——宁缺勿滥。

## 模块结构

| 模块 | 职责 |
|---|---|
| `signals.py` | `Signal`（delivery_key 刻画内容）+ `SignalSource` Protocol + 三种实现：`GitStatusSource`（脏工作区/分支变化）、`TestResultsSource`（pass 率恶化，外部 report() 喂入）、`FileChangeSource`（内容 hash 变化） |
| `judge.py` | `LLMJudge`：prompt 内置四条标准 + 反例，结构化输出 `{"worth_prompting", "reason"}`；解析失败/LLM 失败一律保守不提示 |
| `gate.py` | `ProactiveGate` 四层门控：去重 → 冷却间隔 → 每日上限（UTC 日界）→ 多时间尺度能量衰减 E(t)=Σα·exp(-Δt/τ) |
| `store.py` | `GateStateStore`：门控状态 JSON 原子持久化（重启不重发） |
| `gatekeeper.py` | `Watchdog.poll_once/run_forever`：编排去重 → 判定 → 门控 → 送达，产出 `WatchdogReport` 账目 |

## 编排顺序（判定管"值不值得"，门控管"礼不礼貌"）

```
信号源 poll（同 key 不重吐）
  → 去重查表（O(1)，在花 LLM token 前拦下重复）
  → Agent 结构化判定（不值得 → 记账跳过）
  → 门控（cooldown / daily-limit / energy → 拦截记账）
  → 送达 → record_delivery → 状态落盘
```

## 关键设计决策（详见 docs/specs/2026-08-29-watchdog-p5b-adr.md）

1. **delivery_key 刻画内容而非时间**：同一脏工作区反复 poll 产出同 key 被去重；状态变化才有新 key。
2. **保守降级**：判定解析失败或 LLM 不可用一律沉默——主动式 Agent 的失败模式是漏报而非骚扰。
3. **多时间尺度衰减**：双尺度 (1h, 24h) 能量模型比每日硬上限更平滑，短时连发快速耗尽容量、间隔拉长自然恢复。
4. **与 P5a 的关系**：共享可测轮询模型与持久化模式但不互相依赖；定期巡检可用 scheduler 提交 watchdog.poll_once（组合非耦合）。

## 使用

```python
watchdog = Watchdog(
    [GitStatusSource(repo_dir), TestResultsSource(), FileChangeSource(paths)],
    judge=LLMJudge(client),
    gate=ProactiveGate(GateStateStore(path).load()),
    store=GateStateStore(path),
    deliverer=send_to_session,  # daemon 会话/TUI 系统消息
)
asyncio.create_task(watchdog.run_forever(poll_interval_s=300))
```
