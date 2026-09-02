# FlowCoder 项目检查报告

> 生成日期：2026-08-29 · 基于全库代码探索

## 一、项目定位

本地优先的**自研 AI 编码助手 Agent 运行时**（对标 Claude Code），Python ≥3.11，MIT 协议，v0.2.0。三种运行形态共用同一引擎：Textual TUI 交互、本地 Starlette daemon（端口 7800 + WebSocket）、headless `-p` 模式。核心卖点是**不依赖任何 Agent 框架**（无 LangChain/LangGraph），围绕 Agent 循环、工具治理、上下文工程、长期记忆四大能力全自研。

## 二、架构总览

```
src/flowcoder/
├── agent/        核心：ReAct 循环 + 恢复快照
├── providers/    Anthropic / OpenAI 双协议适配
├── context/      上下文工程：压缩、卸载、恢复
├── permissions/  权限治理 + 路径沙箱
├── memory/       长期记忆（语义召回）
├── teams/        多 Agent 协作
├── mcp/          MCP 客户端 + 工具接入
├── daemon/       本地服务端 + WebSocket 推流
├── skills/ hooks/  声明式技能 + 生命周期钩子
└── tui/ cli/     用户界面层
```

技术栈刻意保持极简：`anthropic`/`openai` SDK、`pydantic v2`、`httpx`、`starlette`+`uvicorn`、`textual`、`mcp`，纯文件持久化（JSON/Markdown），无数据库、无消息队列。

## 三、核心功能与实现方式

**1. Agent 核心循环**（`agent/core.py`，1030 行）
自研异步生成器循环：注入上下文 → 压缩检查 → pre_send 钩子 → LLM 调用 → post_receive 钩子 → 工具授权/执行 → 记忆提取，直到收敛或 max_iterations=50。`run()` 产出 15+ 种事件（StreamText、ToolUseEvent、PermissionRequest…），TUI/Daemon/GUI 统一消费这套事件流——这是整个引擎的骨架。

**2. 多协议流式适配**（`providers/`）
手写 Anthropic Messages、OpenAI Compat、OpenAI Responses 三种 SSE 协议解析（含 content_block 拆解、StreamCollector 聚合），统一成内部事件模型，支持流式工具调用。

**3. 上下文工程**（`context/`，亮点模块）
滑动窗口 + auto_compact（压缩阈值、摘要生成、工具调用配对对齐）+ 工具结果卸载 + 断路器防压缩风暴；`context/recovery.py` 支持会话恢复时按预算重建文件/技能上下文，配合 `/rewind` 回退。

**4. 工具治理与权限**（`permissions/`）
路径沙箱（resolve 后 relative_to 白名单校验）+ 四模式审批（自动/询问/拒绝/总是允许）+ dangerous 命令规则拦截。Bash 工具以 subprocess 执行（600s 超时）——**无 Docker 级隔离，是最大安全短板**。

**5. 多 Agent 协作**（`teams/`）
Coordinator 模式 + mailbox 通信 + 任务管理器，子 Agent 支持进程内/tmux/iTerm2 三种 spawn 后端。

**6. 记忆系统**（`memory/`）
MemoryHub 可插拔长期记忆，`recall.py` 语义召回，记忆提取挂载在循环尾部自动触发。

**7. 扩展体系**
MCP 客户端 + 管理器 + 工具包装；声明式 Skill（frontmatter 解析）；HookEngine 提供 pre_send/post_receive 等生命周期钩子。

**8. Daemon 服务**（`daemon/`）
Starlette + WebSocket 推流，ActiveTaskRegistry/PendingPromptRegistry 管理并发会话任务，支持断点恢复（conversation_snapshot + response_history）。

## 四、工程化水平

- **规模**：345 个 .py 文件，源码约 2.95 万行，测试约 2 万行（占源码 68%，unit 61 文件 + integration 34 文件 + e2e 仅 1 文件）
- **质量工具链**：pytest + pytest-asyncio（auto 模式）、ruff、GitHub Actions CI/Release
- **文档**：15 篇架构文档（agent-loop、context-memory、permissions-hooks 等专题）
- **已知问题**：`app.py` 1967 行 god file；`scripts/benchmark.py` 是空壳占位；限流仅有 RateLimitError 识别与 retry_after 解析，无主动限流器

## 五、能力矩阵

| 能力 | 状态 | 能力 | 状态 |
|---|---|---|---|
| 自研 ReAct 循环 | ✅ | Docker 沙箱 | ❌ |
| SSE 流式解析（3 协议） | ✅ | 评测基准（eval） | ❌（空壳） |
| 上下文压缩/恢复 | ✅ | e2e 测试 | ⚠️ 仅 1 个文件 |
| 断点恢复/回退 | ✅ | 主动限流/指数退避引擎 | ⚠️ 很浅 |
| 多 Agent 协作 | ✅ | OpenTelemetry 可观测 | ❌（自研 TraceManager） |
| MCP / Skill / Hook | ✅ | 数据库/MQ | ❌（纯文件，定位使然） |
| 路径级权限沙箱 | ✅ | Docker 部署 | ❌（本地工具定位） |

## 六、总体评价

介于"高质量个人作品"与"准生产框架"之间：Agent 循环、上下文工程、流式协议解析三个模块达到可精读范本的水准，架构分层清晰、测试比例健康；短板集中在工程外围——无执行隔离沙箱、评测体系缺失、e2e 几乎为零、限流很浅。定位是本地单用户工具，不具备多租户/水平扩展能力。

## 七、后续改造方向（增量贡献点）

1. **Docker 沙箱池**（第 1–2 周）：容器预热租借、cgroup 资源限额、超时强杀、泄漏回收；bash/edit 工具切到沙箱执行，填补最大安全短板。
2. **真实评测流水线**（第 3 周）：将 `scripts/benchmark.py` 空壳落地为 HumanEval+ / SWE-bench Lite 子集评测，产出 pass@1、平均自愈轮次、单题 token 成本、失败模式分类。
3. **并发与可靠性压测**（第 4 周）：池耗尽排队、kill -9 容器、LLM 超时故障转移等混沌场景演练，整理真实数据。
4. **机制/策略分离重构**（进阶）：将循环、权限门、上下文管理、事件流协议下沉为通用 Agent 框架层，编码工具与 prompt 留在应用层；以"换领域只写新工具集"验证抽象成功。
