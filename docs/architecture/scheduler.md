# 调度器（scheduler/）

`src/flowcoder/scheduler/` 提供cron 驱动的自动化值守（P5a）：定时把任务
提交为 daemon 会话里的 Agent 回合。

## 模块结构

| 模块 | 职责 |
|---|---|
| `cron.py` | 5 字段 cron 自实现（零依赖）：解析 + `next_after` 严格后继计算 + 区间触发计数 |
| `latency.py` | `LatencyTracker`：触发延迟滚动窗口、自实现 P90、预触发窗口（clamp 到上限） |
| `store.py` | `ScheduleStore`：任务定义/调度状态/运行记录 JSON 持久化（原子写、损坏兜底、重启恢复） |
| `runner.py` | `Scheduler`：`poll_once(now)` 纯推进函数 + `run_forever()` 守护循环；防抖合并、失败重试、预触发 |
| `daemon_job.py` | `DaemonJobExecutor`：`init_session` + `start_task` 复用 daemon 任务注册表与执行路径 |

## 一次触发的流程

```
add_job(name, cron, prompt)          # next_run 由 cron 计算，持久化
run_forever()
  → 睡到 min(next_run) - 预触发窗口（P90）
  → poll_once(now)（逐任务）
     ├─ 到期判定：now >= next_run - P90 窗口（软实时预触发）
     ├─ 防抖：停机错过的 N 个窗口合并为一次执行，N-1 记入 RunRecord.coalesced
     ├─ 执行：失败按指数退避重试至上限；耗尽记 retry_exhausted
     ├─ 延迟采样：只采触发路径的小延迟（超过预触发上限的巨量延迟 = 停机，不采）
     └─ next_run 推进到下一个未来窗口，状态落盘
```

## 使用

```bash
# 演示（日志 executor，真实时钟）：
python -m flowcoder.scheduler --cron "* * * * *" --prompt "每日巡检"

# daemon 集成（生产）：
executor = DaemonJobExecutor(daemon_server)
scheduler = Scheduler(ScheduleStore(path), executor)
scheduler.add_job("nightly", "0 2 * * *", "夜间依赖巡检")
asyncio.create_task(scheduler.run_forever())
```

## 关键设计决策（详见 docs/specs/2026-08-29-scheduler-p5a-adr.md）

1. **cron 自实现**：只需解析 + next_after 两个纯函数，~150 行零依赖、全边界可测；APScheduler 的执行器抽象与本项目异步生成器风格冲突。
2. **防抖合并**：错过窗口补跑 N 次旧任务无意义，合并为一次并记账。
3. **P90 预触发**：唤醒时刻 = 计划时刻 - 实测延迟 P90，把实际执行点拉回窗口内；延迟采样只限触发路径（巨量延迟是停机不是拥塞）。
4. **触发层/执行层分工**：调度器对"任务"负责到提交成功；Agent 回合结果由 daemon 会话台账追踪。
