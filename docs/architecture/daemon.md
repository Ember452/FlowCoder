# 本地守护进程

Starlette daemon：session 管理、任务调度、A2A 桥接、Origin 鉴权、token 鉴权。

## Outbox 事件投递（P5c）

会话事件持久化层（`events.jsonl`）即 Outbox：`SessionRecords.emit` 时为
事件盖 `seq`（会话内单调）与 `ts` 章并落盘，然后才推送给 WS 客户端。

- **断点补投**：`GET /api/stream/{sid}?since=<seq>` 只补投 seq > since 的事件；
  不带 since 时回退到客户端上次 ack 的持久化游标。信封带 `event_id`（=seq）。
- **ack 记账**：客户端发 `{"action": "ack", "seq": N}`，单调取大写进 session
  meta（重启存活），并清理"结果未知"账本。
- **结果未知不重放**：`PermissionRequest`/`AskUserRequest` 绑定连接生命周期的
  future，已推送未 ack 时补投跳过（保守投递语义）；未决交互由 pending-prompt
  机制重新出账。渲染类事件幂等、始终重放。
- **保留期清理**：默认 72h（`FLOWCODER_OUTBOX_RETENTION_HOURS` 可调），守护
  任务周期执行；只删"已 ack 且过期"事件，未投递不误删；无盖章的历史行保留。
- 调度器（scheduler/）定时触发、看门狗（watchdog/）主动提示，均复用本
  事件流与会话台账；P5.5 起按 `scheduler`/`watchdog` 配置段在 app lifespan
  自动装配（默认全关，装配集中在 daemon/background.py）。

细节与决策：docs/specs/2026-08-29-outbox-p5c-adr.md。

