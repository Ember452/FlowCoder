# 全局架构

FlowCoder 是本地优先的自研 Agent 运行时（TUI + daemon + headless CLI），
四层分层：核心抽象、Agent 引擎、能力扩展、运行形态。

## 分层与模块

```
┌─ 运行形态 ─────────────────────────────────────────────┐
│  ui/ gui/（Textual TUI）   daemon/（Starlette + WS）   │
│  __main__.py（headless CLI）                           │
├─ Agent 引擎 ──────────────────────────────────────────┤
│  agent/          ReAct 主循环、事件流、权限授权、       │
│  │               预算闸（budget.py，P3）               │
│  teams/ mcp/ hooks/ skills/ memory/                    │
├─ 能力扩展 ────────────────────────────────────────────┤
│  tools/          内置工具（bash 工具含沙箱双通道，P1）  │
│  sandbox/        Docker 容器池：预热租借/销毁重建/     │
│  │               回收/指标（P1a/P1b）                  │
│  eval/           HumanEval+ 评测流水线（Agent 的       │
│  │               消费者，P2a/P2b）                     │
│  context/ permissions/ worktree/ a2a/ filehistory/ …  │
├─ 核心抽象 ────────────────────────────────────────────┤
│  client/         LLM 多协议适配 + 韧性层（P3）         │
│  │               （ResilientClient：重试/限流/超时）   │
│  providers/      三协议请求构造与流式解析              │
│  config/         YAML 加载/校验/多层合并               │
│  core/           cache/serialization/driver 等基础设施 │
└───────────────────────────────────────────────────────┘
```

依赖方向（硬规则）：上层可依赖下层，下层禁止 import 上层；
`client/` 与 `config/` 是最底层。跨模块通信优先走 agent 的事件流。

## 一次请求的主干

```
用户输入（TUI/daemon/CLI）
  → ConversationManager（对话状态层）
  → Agent.run()：预算判定 → 压缩检查 → 权限授权 → LLM 调用
     → ResilientClient（限流 → 重试 → 超时兜底 → 协议客户端）
  → StreamCollector → 事件流（StreamText/ToolUseEvent/UsageEvent/…）
  → 工具执行（权限门 → 沙箱/子进程双通道）→ 结果回填 → 下一轮
  → 无工具调用 → LoopComplete
```

细节：数据流全图见 data-flow-and-agent-loop.md；各能力面的模块结构
与设计决策见 sandbox.md / eval.md / llm-client.md / agent-loop.md /
config.md，决策原始记录在 docs/specs/ 的六篇 ADR。

## 改造阶段（P0.5–P4）落点

| 能力 | 模块 | 状态 |
|---|---|---|
| 质量修复 | 12 项 P0 安全/挂死问题 | v0.2.1-quality-fixes |
| Docker 沙箱 | sandbox/（单容器 → 池化 → 接入 Bash 工具） | v0.3.0-sandbox |
| 评测流水线 | eval/（HumanEval+ / 自愈 / k-sample / 失败四分类） | v0.4.0-eval |
| 可靠性加固 | client/resilience.py + agent/budget.py + e2e | v0.5.0-hardening |

规划中未开工：扩展阶段（scheduler/watchdog/Outbox，P5a-c）、
框架抽象 keel/（R1–R4，见 docs/plans/FRAMEWORK_REFACTOR_PLAN.md）。
