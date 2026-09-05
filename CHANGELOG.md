# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 规范。

## [Unreleased]

### Added
- 结构整理（详见 docs/plans/QUALITY_OPTIMIZATION_PLAN.md 结构评审节）：
  下沉三处放错位置的低层模块修复反向依赖（ui/plan_paths→agent/、
  scheduler/cron→core/、daemon 预算构造→agent/budget）；agents/ 包
  改名 subagents/ 消除 agent/agents 双包歧义；team 系工具收进
  tools/teams/ 子包；prompts.py 归入 agent/

### Added
- 韧性层（P3）：`client/resilience.py`——`ResilientClient`（429/5xx/网络错误/超时
  指数退避重试，流式"零交付"失败才可重放）、进程内异步令牌桶限流（RPM）、
  单请求超时兜底；`create_client` 工厂统一包裹，provider 级
  `max_retries` / `rate_limit_rpm` / `request_timeout_s` 配置
- 四维预算闸（P3）：`agent/budget.py` + Agent 最小接入——token/轮次/时间/成本
  超限触发"总结并收敛"而非硬杀，默认不设预算
- 混沌演练框架（P4）：`scripts/chaos.py` 五场景（池耗尽、容器 kill 自愈、
  LLM 断网 30s、429 风暴、预算超限），实测报告 docs/reports/chaos-report.md（5/5 通过）
- 质量加固（2026-09 大厂对标审查，详见 docs/plans/QUALITY_OPTIMIZATION_PLAN.md）：
  CI 新增 e2e 执行、覆盖率 fail-under=62 门禁、pip-audit 依赖漏洞扫描 job；
  提交 uv.lock 使构建可复现；Bash 工具新增 `aclose()` 公共关池入口，
  TUI 退出时关闭沙箱池避免预热容器残留

### Changed
- 新增错误类型：`ServerError`（5xx，可重试）、`LLMTimeoutError`；
  错误映射将 5xx 从一般 API 错误中分离
- 安全命令白名单收紧：`env`/`printenv` 不再免审批；安全命令含 `$` 一律转审批
  （防 shell 变量展开泄漏密钥）；`cat`/`head`/`tail`/`grep` 等读文件命令参数指向
  凭据类路径（`.flowcoder`/`.ssh`/`.aws` 等）时转人工确认
- 含 api_key 的 config.yaml 落盘改为 mkstemp（0o600）+ 原子替换，
  不再以组/其他用户可读的默认权限存在
- teams/worktree 工具链事件循环阻塞清理：git/tmux 子进程与 SessionManager
  同步文件 IO 全部 `asyncio.to_thread` 化，删除以 `asyncio.sleep` 掩盖竞态的写法
- CancelledError 取消语义统一：等待子任务改用 `gather(..., return_exceptions=True)`
  （吞子任务取消、放行自身取消），任务边界标记状态后 re-raise，不再吞取消

### Tests
- e2e 补课（P3）：TUI 关键路径（Textual run_test 驱动真实装配）、daemon 断线
  重连（离线事件按序补投 + 重启恢复）、沙箱任务端到端（docker marker）
- 密钥泄漏通道回归测试（安全命令白名单收紧）；Bash `aclose()` 关池测试

## [0.4.0] - 2026-08-29

### Added
- 评测流水线（P2a/P2b）：`src/flowcoder/eval/`——HumanEval+（EvalPlus 格式）
  加载器（sha256 校验、special-oracle 题跳过）、`python -m flowcoder.eval` CLI
- 自愈闭环：失败输出喂回 Agent 同一会话，最多 3 轮修复；逐轮记录 token 与结果
- k-sample 首胜：同题并行 k 个独立 trial，首个通过即 cancel 其余
- 失败四分类 `failure_tax.py`：编译错 / 逻辑错 / 测试理解错 / 超预算（启发式）
- 温度固定：`ProviderConfig.temperature` 透传三协议，评测默认 0.0（可复现）
- 对比报告：无自愈 vs 有自愈、k=1 vs k=3（逐题通过矩阵）

## [0.3.0] - 2026-08-29

### Added
- Docker 沙箱（P1a）：单容器执行、cgroup 限额（--memory/--cpus/pids-limit）、
  默认断网（network_mode=none）、只读根 + tmpfs 白名单、双层超时
  （容器内 timeout + asyncio 外层兜底）
- 容器池（P1b）：预热租借 O(1)、归还即销毁重建、Condition 排队背压 +
  max_queue 快速失败、租借健康体检、LeaseReaper 孤儿容器回收、聚合指标
- 工具链接入（P1c）：Bash 工具 `sandbox_mode: off | docker`（默认 off 零改动），
  白名单工作目录挂载，`/sandbox` 斜杠命令切换并持久化，Docker 不可用显式报错

### Security
- 权限四模式审批门结构性先于沙箱执行（授权层先于 tool.execute）
- 沙箱默认 non-root + 只读根 + 断网 + 三维限额，纵深防御

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
