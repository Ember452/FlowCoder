# Agent 核心运行循环走读笔记

> P0 阶段产出（2026-08-29）。基线：main @ 6e371d5。
> 每个结论后附源码行号，可逐条核对。行号随代码演进可能漂移，以结论对应的代码语义为准。

## 一、模块结构

`agent/` 包以 `core.py`（约 1000 行）为核心，其余文件都是按职责拆出的协作模块——
`Agent` 类只保留编排逻辑，具体动作全部委托出去：

| 模块 | 职责 | 被谁调用 |
|---|---|---|
| `core.py` | Agent 类：`run()` 交互主循环 + `run_to_completion()` 子 Agent 非交互循环 | TUI / daemon / teams |
| `events.py` | `AgentEvent` 联合类型（15 种事件，events.py:160-175） | 所有消费方 |
| `stream.py` | `StreamCollector`：消费底层 StreamEvent，双向产出（见决策 6） | core.py:436 |
| `llm_preparation.py` | Layer1 入口 `prepare_api_conversation` + deferred 工具提醒注入 | core.py:418,427 |
| `compaction.py` | 上下文注入 / 压缩后重建 / CompactNotification 构造 | core.py:287,350 |
| `tool_execution.py` | 工具执行与批分区（`partition_tool_calls` / `execute_direct_tool_call`） | core.py:512,669 |
| `tool_authorization.py` | 权限预检状态机（deny/ask/allow，不执行工具） | core.py:682 |
| `tool_hooks.py` / `hook_events.py` | pre/post_tool_use 与生命周期钩子的运行与事件出队 | core.py:246,520,617 |
| `tool_results.py`（agent 包内） | tool_result 块/事件的构造 | core.py:576,626 |
| `output_recovery.py` | max_tokens 触顶后的恢复决策 | core.py:460 |
| `response_history.py` / `recovery.py` | 响应写历史、文件历史快照、工具结果恢复快照 | core.py:474,504,764 |
| `usage.py` | token 用量累计 | core.py:449 |
| `memory.py` | `AgentMemoryBridge`：循环与 MemoryHub 的适配层 | core.py:173,260 |
| `notifications.py` | Team mailbox / 外部通知注入 | core.py:654 |

两条循环路径并存且**逻辑有意不完全共享**：

- `run()`（core.py:272）：交互主循环，异步生成器，逐事件 yield；
- `run_to_completion()`（core.py:834）：子 Agent 非交互执行，无事件流，改用
  `event_callback` 回调（core.py:927-938），工具走 `execute_noninteractive_tool_call`
  （core.py:990-999）。为什么不复用 `run()`：子 Agent 不需要权限交互/事件订阅，
  强行共用会把 future 等待逻辑泄漏进非交互路径。

## 二、事件流全图

```
run(conversation)                                # core.py:272
 │
 ├─ 注入上下文：环境 + FLOWCODER.md 指令 + 记忆    # core.py:278-292 (compaction.py:16-24)
 ├─ session_start hook → yield HookEvent          # core.py:295
 │
 ╔═ while True（iteration 上限 50）══════════════ # core.py:302-310
 ║  ① turn_start hook                            # core.py:313
 ║  ② 注入外部通知（Team mailbox 等）              # core.py:317
 ║  ③ Layer2 压缩检查                             # core.py:321-372
 ║  │    达阈值 → yield CompactStarted            # core.py:327
 ║  │    auto_compact() → yield CompactNotification / UsageEvent / ErrorEvent
 ║  ④ pre_send hook                              # core.py:375
 ║  ⑤ 构建 system prompt（每轮重建，支持协调者模式）# core.py:381-392
 ║  ⑥ Plan 模式：注入计划提醒 + 限制权限白名单       # core.py:395-405
 ║  ⑦ deferred 工具提醒（ToolSearch 引导）          # core.py:418
 ║  ⑧ Layer1：prepare_api_conversation            # core.py:427
 ║  ⑨ 调用 LLM 流式                               # core.py:434-437
 ║  │    StreamCollector → yield StreamText / ThinkingText / ToolUseEvent
 ║  ⑩ post_receive hook                          # core.py:442
 ║  ⑪ 累计用量 → yield UsageEvent                 # core.py:449-455
 ║  ⑫ max_tokens 触顶恢复：需要则 continue 重试     # core.py:460-470
 ║  │
 ║  ├─ 无 tool_call → 收敛结束                    # core.py:473-501
 ║  │    写最终响应 + 后台记忆观察/提取 + turn_end/session_end hook
 ║  │    + 文件历史快照 → yield LoopComplete → break
 ║  │
 ║  └─ 有 tool_call → 工具执行                    # core.py:504-644
 ║       partition_tool_calls 按并发安全分区       # core.py:512
 ║       ┌─ 并发批（只读工具）：                    # core.py:514-577
 ║       │   先逐个 pre_tool hook + 权限预检（可 yield PermissionRequest/AskUserRequest）
 ║       │   → asyncio.gather 并行执行            # core.py:554
 ║       │   → 按原始顺序逐个 post_tool hook + yield ToolResultEvent
 ║       └─ 串行批：每个调用完整走                  # core.py:579-627
 ║           pre_tool hook → _authorize_tool → 执行 → post_tool hook → yield 结果
 ║       连续未知工具 ≥3 → ErrorEvent + break      # core.py:629-633
 ║       ExitPlanMode 被调用 → TurnComplete + LoopComplete + break  # core.py:635-640
 ║       turn_end hook → yield TurnComplete        # core.py:642-644
 ╚═══════════════════════════════════════════════
```

事件交互协议：`PermissionRequest` 和 `AskUserRequest` 事件内携带
`asyncio.Future`（events.py:137-157）。Agent yield 后**挂起等待**，调用方
（TUI 弹窗 / daemon WebSocket）把用户决定 `set_result()` 回来，循环才继续
（tool_authorization.py:62-70、core.py:730-744）。这就是"提问"不靠 side-channel
而走统一事件流的原因——任何前端都能用同一套协议接住。

## 三、关键设计决策的"为什么"

### 1. 为什么是异步生成器事件流

`run()` 是 `AsyncIterator[AgentEvent]`（core.py:272）。TUI、daemon、GUI、
headless 四种形态消费同一套事件（events.py 模块 docstring 1-13），引擎不关心
渲染。代价是调用方必须完整消费迭代器才能驱动循环——这是与 daemon
`ActiveTaskRegistry` 配合设计的，不是偶然。

### 2. 为什么每轮重建 system prompt

`build_system_prompt` 在 while 循环内每轮调用（core.py:381-392），而不是启动时
构建一次。因为 Hook 的 prompt 注入（core.py:379）、协调者模式的子 Agent 目录
（core.py:391）都可能逐轮变化；环境上下文则不同——它注入对话历史而非
system prompt（core.py:278-292），压缩后需要显式重注入（core.py:350-355）。

### 3. 为什么工具按并发安全分区

`partition_tool_calls`（core.py:512）把工具调用分成"可并行只读批"和"串行批"。
并发批的流程刻意分成三段：**先逐个做 pre hook + 权限预检**（core.py:517-549，
不执行工具），**再 gather 并行执行**（core.py:554），**最后按原始调用顺序跑
post hook 并汇总**（core.py:556-577）。为什么不在 gather 里做全部步骤：
权限的 ask 交互必须串行弹窗；post hook 若在并行任务里跑，Hook 的副作用顺序
不可预测。结果按原始顺序汇总保证对话历史的确定性。

### 4. 为什么连续未知工具 ≥3 就终止

LLM 幻觉出不存在的工具名时，每轮都会失败重试。`consecutive_unknown`
计数（core.py:299,611-614），连续 3 次直接终止（core.py:629-633）。阈值取 3
而非 1：允许模型自我纠正一次两次，但不给它无限烧 token 的机会。

### 5. 为什么权限门在工具执行之前且无法绕过

`_execute_tool`（core.py:690）先走 `_authorize_tool`（core.py:695-708），拿到
approved 才查 registry、执行。`_execute_single_tool_direct`（core.py:667）是
"仅执行"版本，注释明确要求调用方先完成授权预检（core.py:668）——它只被并发批
在预检通过后调用（core.py:673-676）。allow_always 会把规则持久化到本地规则文件
（tool_authorization.py:82-90），是唯一"免问"通道。

### 6. 为什么 StreamCollector 是"双向产出"

`consume()` 一边把 `TextDelta/ThinkingDelta/ToolCallComplete` 转成前端事件
即时 yield（stream.py:74-100），一边把完整状态累积进 `self.response`
（stream.py:77,84-95,101-107）。单次遍历同时服务实时展示与事后写历史，避免
"先攒完再播"的延迟或"播完再攒"的二次遍历。ThinkingDelta 只转发不累积，
完整思考块在 `ThinkingComplete` 时才入库——签名也在那时带回
（stream.py:80-86），供下一轮 API 回传。

### 7. 为什么记忆是 fire-and-forget

轻量观察在每轮收敛后 `asyncio.ensure_future`（core.py:480-483），重量级 LLM
提取每 5 轮一次（`MEMORY_EXTRACTION_INTERVAL = 5`，core.py:125,484-487）。
为什么后台：两者都是 LLM 调用，阻塞主循环会让用户白等。已知代价：裸
`ensure_future` 无异常兜底、无引用持有——这是 P0.5 审计项（后台任务需入
`self._bg_tasks` 集合），见 CODE_QUALITY_AUDIT.md。

### 8. 为什么 max_tokens 触顶不报错而是续写

`handle_output_token_limit`（core.py:460-470）在 stop_reason 触顶时提输出上限、
注入续写提示，`continue` 重进循环（含 thinking 块回传）。对长代码生成这是
常态而非异常，硬报错会把可恢复状态变成用户可见失败。

### 9. 为什么 Plan 模式在循环内逐轮注入

Plan 模式下每轮重算 plan 文件路径与存在性（core.py:395-405），路径缓存于
`_plan_path_cache`（core.py:206-212）。为什么逐轮：模型可能在轮间创建/更新
计划文件，提醒内容（`build_plan_mode_reminder` 带 iteration 参数）需要反映
当前轮次；同时每次都把 plan 路径同步给 permission_checker 做 EscPlanMode
白名单（core.py:398-400）。

### 10. Layer1/Layer2 的调用点

循环内压缩检查固定在 pre_send 之前（core.py:321-344），Layer1 的
`prepare_api_conversation` 固定在紧贴 LLM 调用之前（core.py:427-431）。顺序
很重要：先决定"历史要不要瘦身"（Layer2），再决定"这一帧怎么发"（Layer1）。
压缩结果可能是 `CompactEvent`（成功）、`None`（不需要/不值得）或错误字符串
（熔断/失败，core.py:371-372 转成 ErrorEvent）——三态都有事件出口，前端全程可见。

## 四、疑点登记（后续阶段处理）

1. `core.py:284` 有遗留 TODO 注释（记忆检索 query 的来源），P0.5 时确认去留。
2. `_execute_tool` 的 `except Exception`（core.py:756-757）兜住所有工具异常转
   error 结果——对工具层是合理兜底；Python 3.11+ 中 `CancelledError` 继承
   `BaseException`，不会被它吞掉，取消语义可以正常传播（已核实，非问题）。
3. `run()` 与 `run_to_completion()` 的压缩/注入逻辑约 60% 重复
   （core.py:278-292 vs 841-901），属既有重复，不属本次任务，仅登记。
