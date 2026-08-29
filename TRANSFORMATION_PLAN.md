# FlowCoder 项目改造方案（Sandbox · Eval · Reliability）

> 目标：在不破坏现有功能的前提下，补齐 FlowCoder 的三大缺口（执行隔离、评测体系、可靠性），产出可量化、可演示、可写进简历的增量贡献。
> 原则：只新增模块，不重写核心；每阶段结束项目都可运行、测试全绿。

## Phase 0：基线与环境（2–3 天）

- 装 WSL2 + Docker Desktop，`.wslconfig` 限内存 12GB
- 精读 `agent/core.py`、`context/manager.py`、`permissions/`，各写一篇走读笔记（面试深挖的底气）
- 建分支 `feat/sandbox-eval`，跑通现有 pytest 基线，记录当前测试通过数作为回归基准
- 借鉴 flow-agent 的习惯：在 `docs/specs/` 按日期写设计决策记录（ADR），每个关键决策留档

## Phase 1：Docker 沙箱池（第 1–2 周，简历最硬模块）

新模块 `src/flowcoder/sandbox/`：

```
sandbox/
├── pool.py          SandboxPool：容器预热、租借/归还、扩缩容
├── container.py     SandboxContainer：执行入口、流式输出回传
├── limits.py        资源限额：cgroup CPU/内存/进程数/PID 上限
├── network.py       网络策略：默认断网，白名单域名走代理
├── reaper.py        泄漏回收：心跳对账，孤儿容器强杀
└── transport.py     文件进出：docker cp / 临时卷（不用目录挂载，规避 WSL2 跨 FS 慢 IO）
```

关键设计决策（每条都是一个面试考点）：

- **预热租借**：维护 N 个已启动容器，执行请求 O(1) 租借，用完销毁重建；池耗尽走 asyncio 队列排队（背压而非拒绝）
- **限额兜底**：容器级 `--memory`/`--cpus`/`pids-limit` + 执行级 asyncio timeout 双层，超时先 SIGTERM 再 SIGKILL
- **安全默认**：断网执行、non-root 用户、只读根文件系统、白名单工作目录；与现有 `permissions/` 四模式审批对接——高危操作先过权限门再进沙箱
- **可观测**：每次执行记录耗时/退出码/资源峰值，挂进 TraceManager

验收标准：bash 工具切到沙箱执行（`sandbox_mode` 默认 off，off 走原 subprocess 路径零改动；
TUI 提供 `/sandbox` 斜杠命令切换并持久化，未装 Docker 时开启给出明确报错）；
沙箱逻辑由 fake docker SDK 的单元测试全量验证（开发环境无 Docker，既定决策）；
真实容器的压测/演示数据（20 容器池压测无泄漏、kill -9 后池自动补充、超时/内存/断网三场景）
为可选验收，待 Docker 环境就绪后补测并回填 ADR。

## Phase 2：评测流水线（第 3 周，把空壳变成真家伙）

改造 `scripts/benchmark.py` 为 `src/flowcoder/eval/` 包。结构借鉴 akashic 的评测分层（dataset / runner / metrics 三段解耦）：

```
eval/
├── datasets/       HumanEval+ 、SWE-bench Lite 子集（各 50 题）的加载器
├── runner.py       并行跑题：每题 k 个 trial 并行采样，asyncio.gather + 信号量限并发
├── metrics.py      pass@1 / pass@k、平均自愈轮次、单题 token 成本、耗时分布
├── failure_tax.py  失败分类：编译错 / 逻辑错 / 测试理解错 / 超预算
└── report.py       产出 Markdown + JSON 报告（可进 git，数字可复现）
```

- 自愈闭环：评测 runner 调用 Agent 循环，把测试失败输出喂回，最多 N 轮修复——这使"自愈提升 pass@1 X%"成为可讲的数据
- 双模型对比跑一组（如主模型 vs 备用模型），顺便验证 providers 层的故障转移

验收标准：一条命令出完整报告；同一配置重跑分数稳定（温度固定）。

## Phase 3：可靠性加固（第 4 周）

- **限流**：借鉴 flow-agent 的 `infra/resilience.py` 思路，建统一 resilience 模块——LLM 调用错误分类（429/超时/5xx）+ 指数退避 + 令牌桶（进程内实现，与 FlowCoder 纯文件定位一致）
- **预算纪律**：token / 轮次 / 时间 / 成本四维预算由引擎强制执行（不是 prompt 恳求），超限触发"总结并收敛"而非硬杀
- **k-sample 首胜**：同一任务并行 k 路 trial，首个通过即胜出、其余 cancel——记录"成本 vs 成功率"实测曲线
- **e2e 补课**：补 3 条 e2e：TUI 全流程、daemon 断线重连、沙箱内任务完成

## Phase 4：混沌演练与数据整理（第 5 周）

场景清单（每个都要有预期行为和实测记录）：池耗尽排队、执行中途 kill 容器、LLM 网络断开 30s、模型 429 风暴、磁盘写满、任务超预算。
产出：`docs/chaos-report.md` + 压测数据表，直接转写成简历数字。

## 扩展阶段：自动化与主动能力（第 6–7 周，可选但强烈推荐）

把助理类项目的三个成熟设计翻译成编码场景（来源：flow-agent / akashic-agent，思想借鉴、代码以自研为主）：

**1. 自动化值守**（新模块 `scheduler/`）
- cron 表达式驱动 Agent 任务：每夜依赖升级检查 + PR 评审、CI 失败自动分诊、定期仓库健康报告
- 任务转 Agent 回合复用 daemon 的任务注册表；运行记录持久化、失败重试、coalesce 防抖（同窗口重复触发合并为一次）
- 收编 akashic 的 LatencyTracker 思想：用 P90 实测调度延迟做软实时预触发

**2. 仓库看门狗（主动式 Agent）**（新模块 `watchdog/`）
- 监听 git 变更 / CI 失败 / 测试恶化等信号，Agent 判断"值得说"才主动提示
- 防骚扰门控沿用 flow-agent ProactiveGate 的三件套：冷却间隔、delivery_key 去重、每日上限；提醒频率用多时间尺度衰减（akashic 电量模型的思想，公式自实现）
- 所有门控状态持久化，重启不重发

**3. Outbox 式可靠事件投递**（改造 `daemon/`）
- WebSocket 事件流落 Outbox：客户端断线期间事件不丢，重连按偏移量补投
- 对"结果未知"的投递不重放（flow-agent 的保守投递语义），防止重复推送
- 事件表持久化 + 保留期清理

**代码复用判定**：resilience 模块、架构边界测试、LatencyTracker 可直接复制改造（先确认源项目 License）；Scheduler/ProactiveGate/Outbox 只抄设计、按 FlowCoder 的异步生成器风格重写；memory 双存储与 LongMemEval 不搬。

## 简历增量（改造完成后可写）

- 设计 Docker 沙箱池：预热租借 + 双层超时 + 心跳对账回收，20 容器压测 0 泄漏，执行冷启动从 X s 降至 X ms
- 构建 HumanEval+ 评测流水线：pass@1 达 X%，自愈机制回收 X% 失败题，失败模式四分类定位改进方向
- 实现 LLM 调用治理：错误分类 + 指数退避 + 令牌桶，混沌演练（429 风暴/断网/杀容器）下任务完成率 100%
- k-sample 首胜策略：单位成本 pass@1 提升 X%（附实测曲线）
- cron 驱动的自动化值守 + 仓库看门狗：事件监听触发主动建议，冷却/去重/限额门控下误报率为 X%，连续运行 X 天 0 重复推送
- Outbox 式事件投递：断线重连按偏移量补投，投递不重放，压测 X 并发会话 0 丢事件

## 明确不做

多租户 / 分布式 / 数据库与 MQ 引入（破坏现有极简定位）/ 办公场景（后续再说）/ GUI。
