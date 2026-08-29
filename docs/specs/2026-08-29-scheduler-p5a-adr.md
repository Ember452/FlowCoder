# ADR：自动化值守调度器（P5a）

- 状态：Accepted（"每分钟任务连跑 10 分钟"的真实时钟验收待用户执行，
  测试内已用加速时间线覆盖等价断言，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P5a、docs/architecture/scheduler.md、daemon.md

## 背景

新建 `src/flowcoder/scheduler/`：cron 驱动定时任务，到期把 prompt 提交为
daemon 会话的 Agent 回合。要求：运行记录持久化、失败重试（上限+退避）、
coalesce 防抖、P90 实测延迟软实时预触发、重启恢复。

## 决策与理由

### D1：cron 自实现，不引入 APScheduler（PROMPTS 指定二选一）

- 调度器只需要两个纯函数：解析校验 + `next_after(t)` 严格后继计算。
  自实现约 150 行、零依赖、时间语义完全可控（naive 本地时间、分钟精度），
  每个边界（闰日/年末/dom-dow OR 语义/别名 7）都有单测。
- APScheduler 带来自己的 executor/event loop 抽象、时区模型与任务存储
  假设，与本项目"asyncio 原语 + 异步生成器事件流"的风格冲突；为两个
  纯函数引入一个调度框架违背极简定位（AGENTS.md：引入依赖需说明理由）。
- 明确不支持：秒级精度、月份/星期英文名（JAN/MON）——解析失败明确报错。

### D2：可测的轮询模型——poll_once(now) 为核心

`Scheduler` 的核心是 `poll_once(now)`：给定时钟做一次到期判定/执行/记账，
无 sleep、无全局状态锁。`run_forever()` 只是 `sleep + poll_once` 的守护
包装。这使"每分钟任务连跑 10 分钟"的验收可以在测试里用合成时钟加速
驱动（逐分钟 poll，断言 10 次运行、无重复窗口、记录完整），而不必真实
等待 10 分钟；真实时钟的连续运行作为用户侧验收项保留。

### D3：防抖合并（coalesce）——错过的窗口补跑一次而非 N 次

停机/事件循环拥塞可能让多个 cron 窗口滑过。语义：合并为**一次**执行，
错过窗口数记入 `RunRecord.coalesced`。
- 为什么不逐窗口补跑：错过的旧任务（如"02:00 夜间巡检"）补跑 5 次通常
  无意义甚至有害（重复副作用）；一次"现在补上"符合值守语义。
- 为什么不直接丢弃：记录 coalesced 数保留可观测性（频繁非零说明调度
  循环拥塞或停机频繁）。
- 实现上 next_run 推进到 `cron.next_after(max(next_run, now))`——防抖后
  绝不连环补跑。

### D4：P90 软实时预触发——唤醒早、判定也要早

- `LatencyTracker` 记录（实际触发 - 计划触发）的滚动窗口（64 样本），
  自实现 P90（排序取 ceil(0.9n)-1 位）。
- 预触发两处生效：守护循环唤醒时刻 = `min(next_run) - window`；
  `poll_once` 的到期判定 = `now >= next_run - window`。只改唤醒不改判定
  是无效的（提前醒来发现"没到期"再睡回去，等于没提前）。
- **采样防污染**（实测发现的缺陷）：停机恢复后的首次触发延迟达分钟级，
  直接采样会把 P90 顶到上限，导致每分钟任务连续触发（测试
  `test_next_run_advances_past_now` 抓出）。规则：只采样
  `delay <= max_pre_trigger_s` 的触发路径延迟；巨量延迟是停机信号，
  由 coalesced 记录承担可观测性。
- 负延迟（预触发早于计划）按 0 计，不反向学习。

### D5：持久化与重启恢复

单 JSON 文件（原子写 tmp+rename；损坏则告警并从空状态开始，不让守护
进程起不来）：任务定义 + 调度状态（next_run）+ 运行记录（滚动上限
200 条）。重启恢复：next_run 从磁盘读回；缺失（首注册/升级）由 cron
重算。防抖 + next_run 恢复共同保证重启后**不重复触发已执行窗口**。

### D6：daemon 集成——触发层与执行层分工

`DaemonJobExecutor` 持有 DaemonServer：懒 `init_session()` 建调度器专用
会话并缓存 sid，每次触发 `start_task(sid, prompt)`——任务进入 daemon 的
`ActiveTaskRegistry`，由 `AgentTaskRunner` 执行，与交互路径同源。会话
失效（daemon 侧清理）时重建一次重试。

语义边界：调度器的 RunRecord 只对"按时触发 + 提交成功"负责；Agent 回合
本身的结果（token/工具/错误）由 daemon 会话的 events.jsonl 台账追踪。
理由：长任务不应阻塞调度循环，触发层与执行层各管各的台账。

## 验收对照

- ✅ cron 解析边界、防抖合并、重试上限、预触发窗口：单测 43 例
- ✅ 每分钟任务 10 分钟无重复触发：加速时间线等价断言（test_basic）
- ⏳ 真实时钟连跑 10 分钟：`python -m flowcoder.scheduler --cron "* * * * *"`，
  运行记录落 `~/.flowcoder/scheduler.json`，留作用户侧验收
- ✅ 重启后恢复：test_restart_recovers_state
