# 可观测性

全链路 trace、响应历史、usage 统计、debug.log。

## 组成

| 能力 | 载体 | 持久化 |
|---|---|---|
| 文本日志（含 trace_id） | `flowcoder.logctx.setup_logging` → 轮转文件 / stderr | `.flowcoder/debug.log` |
| trace 上下文 | `flowcoder.logctx`（contextvar + logging Filter） | 随调用链，不落盘 |
| 子 Agent 调用树 | `flowcoder.subagents.trace.TraceManager` | 内存态，进程退出即失 |
| token 用量 | `flowcoder.agent.usage`（累计 + UsageEvent） | 事件流 / 会话记录 |
| 预算闸 | `flowcoder.agent.budget`（token/轮次/时间/成本四维） | 进程内 |
| 事件流 | `Agent.run()` 事件模型 → daemon WebSocket | `daemon/session/` 会话持久化 |
| 健康检查 | daemon `GET /api/health` | — |

## trace_id 生命周期

一次顶层调用的日志关联靠 trace_id 贯穿：

1. **生成**：根 `Agent.run()` 启动时若 `self.trace_id` 为空则生成 12 位 hex（`logctx.new_trace_id()`）。
2. **绑定**：`logctx.bind_trace_id` 写入 contextvar，整个 `run()` 期间生效；
   生成器关闭（含异常与取消）时在 `finally` 中 reset，不向调用方上下文泄漏。
   asyncio 任务各自持有上下文副本，兄弟任务互不串扰。
3. **继承**：子 Agent 由 `tools/agent/runtime.py` 挂上父级 trace_id
   （`resolve_parent_trace_id`：父的 trace_id，缺省回退父的 agent_id），
   子 Agent 自己 `run()` 时绑定同一 id——同一棵调用树的日志共享 trace_id。
4. **注入日志**：根 handler 挂 `TraceIdLogFilter`，每条日志自动带
   `[trace_id=xxx]` 字段；未绑定（Agent 循环外的代码）时为 `[trace_id=-]`。

排障路径：从 daemon 会话或任务记录拿到时间点 → 在 debug.log 里按 trace_id
过滤出该次调用的全部日志 → 需要结构化数据（token、工具次数、父子关系）时查
TraceManager / 会话记录。

## 日志配置

三个进程入口统一调用 `setup_logging`（格式含 trace_id，重复调用以最后一次为准）：

| 入口 | 输出 |
|---|---|
| `flowcoder/__main__.py`（CLI/TUI） | `.flowcoder/debug.log`，RotatingFileHandler：单文件 10MB、保留 3 个历史 |
| `flowcoder.daemon.server.run_daemon` | stderr（交由容器/前台看） |
| `flowcoder.scheduler.__main__` | stderr |

格式：`%(asctime)s %(name)s %(levelname)s [trace_id=%(trace_id)s] %(message)s`。

约束：`logctx.py` 是最底层基础设施，禁止 import 任何 flowcoder 上层模块。

## 用量与成本

- `agent/usage.py`：按响应累计 input/output token（含 cache 读写计入 context_tokens），
  产出 `UsageEvent` 进事件流，前端可实时展示。
- `agent/budget.py`：四维预算（`max_total_tokens` / `max_turns` / `max_seconds` /
  `max_cost_usd`，成本需配单价）。超限不硬杀——注入收敛请求并撤下工具 schema，
  给模型一轮收尾机会，收敛轮仍超限才强制结束。

## 已知边界与后续

- TraceManager 仅内存态：无法回看历史会话的调用树；trace 不出进程。
- 无运行时指标（延迟分解、token 流量、工具耗时分布）；eval 流水线的
  pass@1 等指标是独立的离线体系。
- 计划中的 OpenTelemetry 桥接（span 层级 agent-loop → llm-call → tool-call →
  sandbox-exec，OTLP 导出）见 `docs/plans/DIFFERENTIATION_PLAN.md` 阶段 D。
