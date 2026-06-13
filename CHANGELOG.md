# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 规范。

## [0.2.0] - 2026-08-01

### Added
- Agent 核心循环：ReAct 推理-行动主循环、工具授权与执行链路、流式输出、中断恢复
- 多智能体 teams 协作：团队编排、子 Agent 委派、mailbox 通信、任务管理器
- 上下文工程：滑动窗口、动态摘要、工具结果卸载、replacement state 管理
- 长期记忆 MemoryHub：会话记忆、自动记忆、语义召回、provider 插件机制
- 工具治理：声明式 Skill 加载、MCP 协议集成、权限四模式审批、钩子引擎
- LLM 客户端：Anthropic / OpenAI Compat / Responses 多供应商适配、流式 SSE、上下文窗口管理
- 本地 daemon：Starlette 服务、session 管理、任务调度、A2A 协议桥接
- TUI 交互：Textual 主应用、权限/计划/会话对话框、teammate 树、样式主题
- 工程化：src layout、pytest 分层（unit/integration/e2e）、ruff、CI 流水线

### Changed
- 项目结构从 flat layout 迁移至 src layout，隔离源码与安装产物
- 核心基础设施（cache/serialization/frontmatter/driver）归入 core 层
- TUI 对话框与样式归入 ui 层

## [0.1.0] - 2026-06-02

### Added
- 项目骨架与构建配置（hatchling）
- 核心基础设施：cache、serialization、frontmatter、driver
- 配置加载与校验框架
