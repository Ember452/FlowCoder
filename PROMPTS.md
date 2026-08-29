# 分阶段改造 Prompt 清单

> 用法：按顺序执行，每个 Prompt 一个独立会话；执行完先做该阶段的"验收关卡"，通过后再进入下一阶段。
> 每个 Prompt 都已内置约束（读文档、测试绿、不主动 commit）。验收失败就把 AI 的产出回滚，带着失败原因重跑同一 Prompt。
> 提交与打 tag 规则见文末"提交与打 tag 节点"，AI 会在每个阶段完成后主动询问是否 commit + tag。

---

## P0 — 基线与环境

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 0、PROJECT_REVIEW.md。

任务：
1. 检查本机 WSL2 与 Docker 环境是否就绪（wsl --status、docker version）；如果未安装，
   给出精确到命令的安装步骤清单让我手动执行，不要尝试自行安装系统级软件。
2. 跑通现有测试基线：ruff check、ruff format --check、pytest tests/unit、tests/integration，
   记录通过/失败数量，写入 docs/specs/2026-XX-XX-baseline.md（日期用当天）。
3. 精读 src/flowcoder/agent/core.py 和 src/flowcoder/context/ 下的核心文件，
   在 docs/development/ 新增两篇走读笔记（agent-loop-walkthrough.md、context-walkthrough.md），
   内容包括：模块结构、事件流全图、每个关键设计决策的"为什么"。这是给后续改造打地基，要求准确，宁缺勿错。
4. 创建分支 feat/sandbox-eval（只建分支，不 commit 任何代码）。

验收：测试基线文档存在且数字真实；两篇走读笔记中的每个结论都能在源码中找到对应行。
```

## P0.5 — 质量修复批次一（安全与挂死，改造前必做）

```text
先完整阅读 AGENTS.md、CODE_QUALITY_AUDIT.md 全文（重点是 P0 的 12 项与修复顺序建议）、
TRANSFORMATION_PLAN.md 的 Phase 0。

任务：修复审查报告中的全部 12 项 P0，按安全类 → 挂死类 → 引擎类顺序，每项：
1. 先写一个复现该问题的失败测试（安全类问题用恶意输入用例，如 sandbox.py 的
   `<root>/x/../../secret.txt` 穿越、hooks 的 `; rm -rf ~` 注入、skills 的 fork 零权限）。
2. 最小 diff 修复（遵守 AGENTS.md 第三节依赖方向；AskUserQuestion 特例下沉这类
   涉及结构调整的，先给方案再动手）。
3. 测试转绿，确认全量 unit + integration 不回归。

修复时的既定决策（不要重新讨论）：
- sandbox.py：fallback 后对完整 real_path 再 resolve(strict=False) 重查，并补
  Windows 盘符大小写归一化的测试用例
- providers：补协议无关的"流收尾保底"——无论何种退出路径都保证恰好一个 StreamEnd
  （含 response.failed/incomplete、usage chunk 缺失、断流三种场景）；
  Responses 的 tools 格式转换对齐 openai_compat_request.py 的既有实现
- core.py fire-and-forget：后台任务保存到 self._bg_tasks 集合，done callback 记日志
- context/manager.py:353 的条件表达式：按原意图改为括号明确的组合，并写测试覆盖
  "含 too many 但与上下文长度无关"的错误不触发丢弃
- app.py:8 发消息竞态：引入单一 asyncio.Queue 串行化 _send_message 的触发源
- app.py:7 adopt_running 死分支：直接删除该分支与 _subagent_task 属性

约束：只修这 12 项，不顺手修 P1/P2；每项修复的 diff 独立可读。
产出：docs/specs/ 写一篇安全修复 ADR（三个安全洞的根因与修法）；
在 CODE_QUALITY_AUDIT.md 的 P0 表格逐项标注"已修复 + 测试名"。
验收：全量测试绿；12 项 P0 每项有对应回归测试；ADR 存在。
```

## P1a — 沙箱：单容器执行与资源限额

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 1、docs/architecture/permissions-hooks.md、
docs/architecture/tools.md、P0 产出的两篇走读笔记。

任务：新建 src/flowcoder/sandbox/ 子包，本阶段只做"单容器执行"，不做池化：
1. container.py：SandboxContainer——用 docker SDK 启动 python:3.11-slim 容器，
   执行一段代码/命令并回传 stdout/stderr/退出码/耗时；non-root、只读根文件系统、工作目录白名单。
2. limits.py：cgroup 资源限额（--memory、--cpus、pids-limit）；network.py：默认断网执行。
3. 双层超时：asyncio 外层超时 + 容器内 timeout 命令；超时先 SIGTERM 再 SIGKILL。
4. transport.py：输入文件用 docker cp 或临时卷传入，不用目录挂载（规避 WSL2 跨文件系统慢 IO）。
5. tests/unit/sandbox/ 与 tests/integration/sandbox/ 补测试：正常执行、超时击杀、内存超限、
   断网逻辑（容器内 curl 失败）。
   **开发环境无 Docker（既定决策）：单元测试全部用 fake docker SDK 实现验证，
   不依赖真实 Docker；集成测试全量挂 docker marker，无 daemon 时自动跳过。**
6. docs/specs/ 写一篇 ADR：说明为什么容器级+执行级双层限额、为什么断网是默认值。
7. docs/architecture/ 新增 sandbox.md，并在 AGENTS.md 第三节目录职责表中登记 sandbox/。

约束：不改 agent/、tools/ 任何现有文件（本阶段不接入，P1c 才接）；不引入新第三方依赖，
docker SDK 若未安装先在 pyproject.toml 可选依赖组中声明。
验收：单元测试（fake）全绿、逻辑闭环；集成测试在无 Docker 环境下显示 skip 而非 fail；
"kill -9 容器后无残留"等真实容器验收延后，待 Docker 环境就绪后补测并回填 ADR。
```

## P1b — 沙箱：容器池化与泄漏回收

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 1、src/flowcoder/sandbox/ 现有代码与测试。

任务：
1. pool.py：SandboxPool——启动时预热 N 个容器（N 可配置，默认 10），执行请求 O(1) 租借、
   用完销毁重建；池耗尽时请求进入 asyncio.Queue 排队（背压），支持配置队列上限。
2. reaper.py：心跳对账回收——记录每次租借的容器 ID 与归属任务，定期对账，
   归属任务已结束但容器未归还的视为孤儿，强制销毁；启动时清理上次运行遗留的孤儿容器。
3. 指标：租借等待时间、容器复用次数、执行耗时、资源峰值，接入现有 TraceManager
   （先读 docs/architecture/observability.md 了解 TraceManager 用法）。
4. 测试：并发 20 请求压池、池耗尽排队行为、手工 kill 容器后池自动补充、
   模拟泄漏后 reaper 回收——全部基于 fake 容器实现做单元验证；
   真实容器的集成版本同样挂 docker marker 自动跳过。
5. docs/specs/ 写 ADR：预热租借 vs 每次冷启动的取舍；队列背压 vs 快速失败的取舍。

验收：fake 驱动的单元测试全绿（池化/排队/回收逻辑闭环）；
"20 并发压测无泄漏（docker ps 对账）、冷启动消除数据（对比 P1a 耗时）"
为真实 Docker 环境的可选验收，待环境就绪后补测，数据回填 ADR 与简历素材。
```

## P1c — 沙箱：接入工具链与权限门

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 1、docs/architecture/tools.md、
permissions-hooks.md、src/flowcoder/permissions/ 与 tools/ 的 bash 工具实现。

任务：把 bash 工具的执行从裸 subprocess 切换到沙箱：
1. 工具层加配置项 sandbox_mode: off | docker（默认 off，保证行为兼容），off 走原路径零改动。
   终端（TUI）提供斜杠命令（如 /sandbox）在会话内切换 off/docker，切换即生效并持久化
   到用户配置；daemon/GUI 侧状态同步展示。未安装 Docker 时 /sandbox 开启 docker 模式
   要给出明确的错误提示，不得静默失败。
2. docker 模式下：命令在沙箱容器内执行，工作目录映射到白名单目录；执行前先过现有
   permissions 四模式审批门，审批语义不变（这是硬要求：权限门在沙箱之前）。
3. 危险命令规则、路径校验逻辑保持生效——先读清楚现有实现再动手，不许绕过。
4. 全部现有 unit/integration 测试必须保持通过（行为兼容是验收的一部分）；
   docker 模式的接入逻辑用 fake 沙箱做单元验证，真实容器集成测试挂 docker marker
   自动跳过。
5. docs/specs/ 写 ADR：为什么默认 off、为什么权限门在沙箱之前而不是之后。

验收：sandbox_mode=off（默认）时所有既有测试通过、行为零变化；
/sandbox 命令切换逻辑与未装 Docker 时的降级提示有单元测试覆盖；
docker 模式"超时脚本被杀、内存超限被限、断网命令失败"三场景演示为可选验收，
待 Docker 环境就绪后补录进 ADR。
```

## P2a — 评测：HumanEval+ 流水线

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 2、docs/architecture/agent-loop.md、
P0 产出的 agent-loop 走读笔记、scripts/benchmark.py 现状。

任务：新建 src/flowcoder/eval/ 包，结构按 dataset / runner / metrics / report 四段解耦：
1. datasets/：HumanEval+ 数据集加载器（支持从本地 JSON 读取，数据文件不入 git，
   提供下载脚本与校验和说明），先取 50 题。
2. runner.py：逐题调用 Agent 循环生成解法，在沙箱中执行测试（复用 sandbox 模块），
   温度固定、每题 1 trial；asyncio.Semaphore 限制并发。
3. metrics.py：pass@1、平均 token 成本、平均耗时。
4. report.py：产出 Markdown + JSON 报告到 eval-results/（目录加 .gitignore）。
5. tests：用 2 道内置玩具题 + fake provider 跑通全链路的单元测试（不依赖真实 LLM）。
6. docs/specs/ 写 ADR：评测为什么走沙箱执行、为什么 dataset/runner/metrics 解耦。

约束：不改 agent/core.py——评测是 Agent 的消费者，不是修改者；如果发现必须改核心循环
才能评测，停下来在报告中说明，等我决策。
验收：fake provider 单测全绿；真实 LLM 跑 50 题产出第一份真实报告。
```

## P2b — 评测：自愈闭环与失败分类

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 2、src/flowcoder/eval/ 现有代码。

任务：
1. runner.py 增加自愈循环：测试失败输出喂回 Agent，最多 N 轮（默认 3）修复重跑；
   记录每轮的 token 消耗与结果。
2. failure_tax.py：失败四分类——编译错 / 逻辑错 / 测试理解错 / 超预算；
   分类规则先基于输出特征写启发式，附说明。
3. k-sample 首胜：同一题并行 k 个独立 trial（可配置，默认 3），首个通过即胜出、
   其余 cancel（asyncio 取消语义要正确，先读 AGENTS.md 第五节）；记录
   "k 值 vs 单位成本 pass@1" 曲线数据。
4. report.py 扩展：自愈回收率、失败分布、k-sample 对比表。
5. tests：自愈轮次上限、cancel 无泄漏、分类命中的单元测试。
验收：50 题全量跑出对比报告（无自愈 vs 有自愈、k=1 vs k=3），数字可复现（温度固定）。
```

## P3 — 可靠性加固

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 3、docs/architecture/llm-client.md、
docs/development/03-LLM客户端与Provider适配层.md、src/flowcoder/client/ 现有错误处理。

任务：
1. 新建 src/flowcoder/resilience.py（或并入 client 包，先判断哪个更符合现有结构）：
   错误分类（429 / 超时 / 5xx / 网络）、指数退避重试（带抖动）、进程内令牌桶限流。
   参考 flow-agent 的 resilience 设计思想，但按本项目的异步生成器风格重写。
2. 预算纪律：在 Agent 循环外围实现 token / 轮次 / 时间 / 成本四维预算闸，
   超限触发"总结并收敛"而不是硬杀（先读 agent-loop 文档找到正确的接入点，
   如果必须改 core.py，改动保持在最小 diff 并单独说明）。
3. e2e 补课：tests/e2e/ 新增三条——TUI 关键路径、daemon 断线重连、沙箱内任务完成。
4. tests：令牌桶并发正确性、退避序列、预算触发的收敛行为。
验收：全量测试绿；用故障注入（monkeypatch provider 抛 429/超时）验证重试与故障转移路径。
```

## P4 — 混沌演练与数据整理

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的 Phase 4 与扩展阶段、前序各阶段产出的 ADR。

任务：
1. 写 scripts/chaos.py：可脚本化的故障注入——池耗尽、执行中途 kill 容器、
   LLM 断网 30s、429 风暴、任务超预算，每个场景定义预期行为。
2. 逐场景执行并记录实际行为 vs 预期，产出 docs/chaos-report.md：
   场景、注入方式、预期、实测、结论；附压测数据表（并发数、P50/P99、恢复成功率）。
3. 任何场景实测不符预期：定位根因、修复、重测，修复计入 commit 正文。
4. 把 TRANSFORMATION_PLAN.md 简历增量中的 X 占位符用实测数字填掉，更新 CHANGELOG.md。

验收：chaos-report.md 中每个场景都有可复现的注入命令和实测结论。
```

## P5a — 扩展：自动化值守调度器

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的扩展阶段、docs/architecture/daemon.md、
docs/architecture/config.md、src/flowcoder/daemon/ 任务注册表实现。

任务：新建 src/flowcoder/scheduler/ 子包：
1. cron 表达式解析（不引入 APScheduler，自实现或用极小依赖，写 ADR 说明选择）。
2. 定时任务驱动 Agent 回合：到期任务复用 daemon 的任务注册表与执行路径；
   运行记录持久化、失败重试（上限+退避）、coalesce 防抖（同窗口重复触发合并为一次）。
3. 借鉴"用 P90 实测延迟做软实时预触发"的思想（akashic LatencyTracker）：记录调度
   实际触发延迟，用滚动 P90 提前预触发，防止错过时间窗口；P90 计算自实现。
4. tests：cron 解析边界、防抖合并、重试上限、预触发窗口计算。
验收：配置一个每分钟任务连续跑 10 分钟，运行记录完整、无重复触发、重启后恢复。
```

## P5b — 扩展：仓库看门狗（主动式 Agent）

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的扩展阶段、docs/architecture/daemon.md、
P5a 产出的 scheduler 代码与 ADR。

任务：新建 src/flowcoder/watchdog/ 子包：
1. 信号监听：git 状态变更 / 测试结果恶化 / 指定文件变更，可插拔的信号源接口。
2. 判定环节：信号触发 Agent 判断"是否值得主动提示"（prompt 内置标准 + 结构化输出）。
3. 防骚扰门控（借鉴 flow-agent ProactiveGate，代码重写）：冷却间隔、delivery_key
   去重、每日上限；提醒频率叠加多时间尺度衰减（E(t)=Σα·exp(-t/τ) 的思想，公式自实现）。
4. 全部门控状态持久化：重启后不重发已发过的提醒。
5. tests：去重命中、冷却拦截、每日上限、重启后状态恢复、Agent 判定为"不值得说"时不提醒。
验收：连续运行 1 天的实测记录——触发次数、拦截次数、0 重复推送。
```

## P5c — 扩展：Outbox 式可靠事件投递

```text
先完整阅读 AGENTS.md、TRANSFORMATION_PLAN.md 的扩展阶段、docs/architecture/daemon.md、
src/flowcoder/daemon/ 的事件流实现、docs/architecture/observability.md。

任务：改造 daemon 的 WebSocket 事件流为 Outbox 模式：
1. 事件先落本地持久化队列（JSONL 或 SQLite，写 ADR 论证选型），再投递给在线客户端。
2. 客户端重连时带上次事件偏移量，从断点补投；"结果未知"的投递不重放
   （借鉴 flow-agent 的保守投递语义，先读懂其设计再按本项目风格实现）。
3. 事件保留期配置 + 过期清理任务。
4. tests：断线期间产生的事件重连后按序补投、不丢不重、保留期清理不误删未投递事件。
验收：压测——推送中途 kill 客户端再重连，事件序列完整校验通过。
```

---

## R1 — 框架重构：事件协议先行

```text
先完整阅读 AGENTS.md、FRAMEWORK_REFACTOR_PLAN.md 全文（重点是第二节分层架构与第四节路线）、
PROJECT_REVIEW.md。确认 TRANSFORMATION_PLAN 各阶段已完成。

任务（重构阶段 1，绞杀者模式第一步）：
1. 新建顶层包 keel/（与 src/flowcoder 同级或在 src/ 下，按项目 src-layout 惯例定，写 ADR 记录决策）。
2. 把 agent 产出的事件类型（StreamText、ToolUseEvent、PermissionRequest 等）抽到 keel/events/，
   flowcoder 改为从 keel import——本阶段只动归属，不改任何行为、不改任何类型定义。
3. tests 全量保持绿；新增 import 层面测试：flowcoder.agent 引用 keel.events 而非本地定义。
验收：git diff 中没有一行行为变更（只有移动与 import 调整）；全量测试绿。
```

## R2 — 框架重构：五端口抽取

```text
先完整阅读 AGENTS.md、FRAMEWORK_REFACTOR_PLAN.md 第二、四节、R1 产出的 keel/events/、
docs/architecture/ 下 agent-loop / llm-client / permissions-hooks / observability 四篇。

任务（重构阶段 2，依赖倒置）：
1. keel/ 定义五个 Protocol：Provider、Tool、Policy、Memory、Sandbox
   （签名按 FRAMEWORK_REFACTOR_PLAN.md 第二节，结合现有代码实际签名微调，偏差写进 ADR）。
2. 逐模块迁移（每个模块一个独立 commit，迁移完跑全量测试再迁下一个）：
   providers → keel.providers（实现 Provider）；permissions → keel.policy（实现 Policy）；
   sandbox → keel.sandbox（实现 Sandbox）；memory → keel.memory（实现 Memory 接口）；
   context/ → keel.context。
3. keel/engine/ 承接循环骨架，只认五个 Protocol，不认识任何"编码"概念。
4. 每步迁移保持 flowcoder 内的调用方改 import 后行为不变。
验收：全量测试绿；keel/ 中 grep 不到 bash / edit / coding 等编码词汇。
```

## R3 — 框架重构：策略剥离与 god file 清算

```text
先完整阅读 AGENTS.md（特别是第四节文件拆分规范与 app.py 禁令）、
FRAMEWORK_REFACTOR_PLAN.md 第二、四节、keel/ 与 src/flowcoder/ 当前结构。

任务（重构阶段 3）：
1. 编码专属物迁入 profiles/coder/（或保留 flowcoder 作为 coder profile，选一种并在 ADR
   记录理由）：bash/edit 工具、编码 system prompt、skills、编码相关的 permissions 策略配置。
2. 拆解 app.py（1967 行 god file）：按 AGENTS.md 第四节的拆分信号逐一处理，
   装配逻辑移到独立 bootstrap 模块；拆分过程中不改任何行为。
3. 更新 docs/architecture/overview.md 与 INDEX.md 反映新结构。
验收：keel/ 无编码概念、flowcoder 全部测试绿、app.py 不再是 god file（各部分可追溯去向）。
```

## R4 — 框架重构：抽象验证（裁判局）

```text
先完整阅读 AGENTS.md、FRAMEWORK_REFACTOR_PLAN.md 第二、四节、keel/ 全部公共接口。

任务（重构阶段 4，抽象是否成功的唯一裁判）：
1. 写 profiles/minimal/：一个 CSV 数据问答 Agent——只依赖 keel（Provider/Tool/Policy/Memory），
   工具就两个：read_csv、run_query（不进沙箱，subprocess + 白名单即可），
   prompt 自带输出规范。禁止修改 keel/ 的任何一行来实现它。
2. 跑通：加载 CSV → Agent 回答"某列的总和/均值/Top-N" → 输出结构化结果。
3. 如果实现中被迫修改 keel/：记录每一处修改，判定为抽象泄漏点，评估是"接口缺口"
   还是"不该有的耦合"，给出修正方案——但本阶段先如实记录，不擅自改接口。
4. 产出 docs/architecture/keel-abstraction-report.md：验证结论、泄漏点清单、接口缺口。
5. 更新 CHANGELOG.md 与 README.md（如 README 需反映新架构），打 tag。

验收：minimal profile 跑通且 keel/ 零改动（这是通过标准）；若存在泄漏点，报告如实列出。
```

---

## 提交与打 tag 节点

AI 在每个阶段验收通过后，**必须主动询问用户是否 commit + 打 tag**，不询问直接提交视为违规（与 AGENTS.md 的 commit 规则一致）。

固定打 tag 节点（回滚锚点，务必保留）：

| 节点 | 时机 | 建议 tag 名 | 锚定状态 |
|---|---|---|---|
| 1 | P0.5 验收通过后 | `v0.2.1-quality-fixes` | 安全且全绿的基线 |
| 2 | P1c 验收通过后 | `v0.3.0-sandbox` | 沙箱完整接入 |
| 3 | P2b 验收通过后 | `v0.4.0-eval` | 评测流水线出真实数据 |
| 4 | P4 验收通过后 | `v0.5.0-hardening` | 可靠性加固 + 混沌演练完成 |
| 5 | P5c 验收通过后（如执行） | `v0.6.0-automation` | 自动化/主动能力完成 |
| 6 | R4 验收通过后 | `v1.0.0-keel` | 框架抽象完成 |

规则：

- 固定节点外的阶段（P0、P1a、P1b、P2a、P3、P5a、P5b、R1–R3）：完成后**先 commit 后询问**是否顺手打 tag，由用户决定；commit 遵循 AGENTS.md 的 Conventional Commits
- tag 一律用 annotated tag（`git tag -a <名> -m "<阶段说明>"`）
- 若某阶段被砍掉（如 P5c 不做），跳过对应 tag 节点，后续 tag 名不变
- 任何阶段验收失败回滚时，回滚目标是最近一个固定 tag

## 使用提醒

- **一次会话一个 Prompt**，不要把两个阶段塞给同一个会话——上下文越长执行越漂
- 每个阶段结束让 AI 汇报：改动文件清单、测试结果、与验收标准的逐条对照，你核对后再进下一阶段
- AI 提出要改核心循环（agent/core.py）或要引入新依赖时，让它停下来给你方案再决策，这两类事不委托
- P 阶段和 R 阶段之间是严格先后：R1 之前必须完成 P2a/P2b（评测要在重构前建立行为基线）
