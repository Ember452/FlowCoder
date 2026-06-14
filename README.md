# FlowCoder

> 本地优先的自研 Agent 运行时：TUI + daemon + headless CLI 三态一体。

FlowCoder 是一个本地优先的 AI 编码助手运行时，围绕 Agent 核心循环、工具治理、上下文工程、长期记忆四大能力构建，支持 TUI 交互、本地 daemon 服务、headless CLI 三种运行形态。

## 特性

- **Agent 核心引擎**：ReAct 推理-行动主循环，工具调用授权、流式输出、中断恢复、响应历史
- **多智能体协作**：teams 团队编排、子 Agent 委派、任务管理器、mailbox 通信
- **工具治理**：声明式 Skill 加载、MCP 协议集成、权限审批、钩子引擎
- **上下文工程**：滑动窗口、动态摘要、工具结果卸载、跨会话长期记忆 MemoryHub
- **多供应商适配**：Anthropic Messages API、OpenAI Compat / Responses API、流式 SSE 解析
- **运行形态**：Textual TUI、Starlette daemon（端口 7800）、headless `-p` 模式

## 安装

```bash
uv pip install -e ".[dev]"
```

## 使用

```bash
# TUI 交互模式
flowcoder

# headless 模式：执行 prompt 并输出结果
flowcoder -p "summarize this project"

# 启动本地 daemon
flowcoder-daemon
```

## 项目结构

采用 src layout + 分层架构：

```
src/flowcoder/
├── core/        # 核心抽象与基础设施
├── agent/       # Agent 核心引擎
├── agents/      # Agent 变体与任务管理
├── teams/       # 多智能体协作
├── context/     # 上下文工程
├── memory/      # 长期记忆
├── tools/       # 能力扩展
├── skills/      # 技能声明式加载
├── mcp/         # MCP 协议集成
├── hooks/       # 钩子引擎
├── permissions/ # 权限审批
├── providers/   # LLM 供应商适配
├── client/      # LLM 客户端
├── config/      # 配置系统
├── commands/    # 斜杠命令
├── daemon/      # 本地守护进程
├── a2a/         # A2A 协议桥接
├── account/     # 云端账号
├── worktree/    # git worktree
├── filehistory/ # 文件历史
├── gui/         # GUI 桥接
└── ui/          # TUI 交互组件
```

## 测试

```bash
pytest tests/unit -q          # 单元测试
pytest tests/integration -q   # 集成测试
```

## License

MIT
