# ADR：Outbox 式可靠事件投递（P5c）

- 状态：Accepted（压测验收：推送中途 kill 客户端重连，事件序列完整校验通过——
  tests/e2e/test_outbox_delivery.py）
- 日期：2026-08-29
- 关联：PROMPTS.md P5c、docs/architecture/daemon.md、docs/architecture/scheduler.md

## 背景

daemon 的 WebSocket 事件流此前是"尽力推送"：事件落 `events.jsonl`（已是
追加日志），但客户端断线期间的事件只在内存 tail 里滚过，重连后全量重放
或靠客户端自己截取，没有游标协议、没有投递结果记账、没有保留期治理。
P5c 把它升级为 Outbox 模式。

## 决策与理由

### D1：JSONL 追加文件，不引入 SQLite

daemon 事件流的持久化层（session store 的 `events.jsonl`）**本身就是**
逐事件 fsync 的追加日志——"事件先落盘再投递"的 Outbox 骨架已经存在
（`_emit` 的注释即此承诺："durability is an event boundary, not task
completion"）。缺的是：seq 单调游标、投递结果记账、按游标补投、保留期
治理。补齐这四件事不需要 SQLite 的事务能力：本场景单写多读、只有追加
和整文件重写两种写模式。P5a 的 JSONL+原子写持久化模式一脉相承。

### D2：seq 在 emit 时盖章，而非发送时编号

事件在 `SessionRecords.emit` 时盖 `seq`（会话内单调）与 `ts`（保留期
判定用）并持久化。两个坑（实测发现）：
- seq 不能取 `len(log)+1`：保留期清理会缩短事件视图，len 不再单调——
  改为"历史最大 seq + 1"；
- `seq: true` 是合法 int 子类——解析时显式排除 bool。
重连协议：`GET /api/stream/{sid}?since=<seq>` 只补投 seq > since 的事件；
不带 since 时回退到**持久化的 ack 游标**（客户端上次确认位置）。
事件信封带 `event_id`（= seq）供客户端记账。

### D3：ack 记账与"结果未知不重放"

- 客户端发 `{"action": "ack", "seq": N}`：server 单调取大写进 session
  meta（持久化，重启存活），同时清掉未知账本中 ≤N 的条目。
- **保守投递语义**：`PermissionRequest` / `AskUserRequest` 绑定连接生命周期
  的 future——已推送但未 ack 时投递结果未知，这类事件重连补投**跳过**
  （跳过一次即"已知跳过"，不再累积）；未决交互由既有的 pending-prompt
  机制重新出账，而不是重放一条 future 已死的事件。普通渲染类事件
  （StreamText 等）即使已推未 ack 也重放——客户端渲染幂等。
- 实现位置：`OutboxLedger`（进程内账本）+ `should_replay` 纯函数；
  交互事件推送即记账（mark_pushed）。

### D4：保留期清理——不误删未投递

删除规则：`seq ≤ acked_seq`（客户端确认收到）**且**年龄超过保留期。
未 ack 的事件无论多旧都保留。无 seq/ts 的历史行（升级前的旧数据）一律
保留——治理不破坏旧数据。清理以整文件重写实现（原子写），零删除时
不重写。默认保留期 72h（`FLOWCODER_OUTBOX_RETENTION_HOURS` 可调），
清理守护任务挂在 app lifespan（1h 周期），`create_app(outbox_retention_s)`
可注入（测试用）。

### D5：现有客户端兼容

不带 since 的旧客户端：since 回退到 ack（初始 0）→ 全量重放，行为与
升级前一致（除信封多出 seq/ts 字段，客户端忽略未知字段即可）。
`ReplayDone` / cancel 动作语义不变。既有集成测试仅更新了断言方式
（剥离盖章字段或按字段比较）。

## 交付物

- `daemon/outbox.py`：seq/ts 工具、OutboxLedger、should_replay、
  cleanup_outbox_file、清理守护循环与 lifespan
- `session/records.py`：emit 盖章、ack 记账（持久化进 meta）
- `routes/stream.py`：since/ack/未知不重放
- `server.py`：lifespan 挂清理任务 + 环境变量配置

## 待办

- [x] 压测验收：kill 中途客户端重连，seq 1..10 连续无缺口无重复
      （tests/e2e/test_outbox_delivery.py）
- [ ] 长稳：真实长时运行下观察 outbox 文件大小与清理周期表现
