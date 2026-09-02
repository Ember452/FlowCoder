# AGENTS.md — FlowCoder 开发规范

面向 AI 辅助开发的行为准则与项目规范。与各任务的临时指示合并使用。

**取舍说明**：以下规范偏向谨慎而非速度。琐碎任务可自行裁量，但拿不准时按规范执行。

---

## 一、行为准则

### 1. 先想清楚再动手

**不要臆测。不要隐藏困惑。主动暴露权衡。**

- 实现前先陈述你的假设；不确定就问。
- 存在多种理解时，把选项摆出来，不要默默替用户选。
- 有更简单的做法就直说，认为方案有问题就提出反对。
- 有不清楚的地方就停下来，指明困惑点，提问。

### 2. 简单优先

**用解决问题的最少代码。不做任何投机性设计。**

- 不做超出要求的功能。
- 单次使用的代码不抽象。
- 不做没被要求的"灵活性"和"可配置性"。
- 不为不可能发生的场景写错误处理。
- 写了 200 行而 50 行能解决的，重写。

自问："资深工程师会觉得这里过度设计吗？"会，就简化。

### 3. 外科手术式修改

**只动必须动的。只清理自己制造的。**

- 不"顺手改进"相邻代码、注释、格式。
- 不重构没坏的东西。
- 跟随既有风格，即使你有不同偏好。
- 发现无关的死代码：提出来，不要删。

当你的修改产生孤儿时：

- 清理**你的修改**导致未使用的 import/变量/函数。
- 既有死代码，除非被要求，否则不动。

检验标准：每一行改动都能直接追溯到当前任务。

### 4. 目标驱动执行

**先定义成功标准，循环直到验证通过。**

把任务转成可验证的目标：

- "加个校验" → "先写非法输入的测试，再让测试通过"
- "修个 bug" → "先写复现 bug 的测试，再让它通过"
- "重构 X" → "重构前后测试都通过"

多步任务先给出简短计划：

```
1. [步骤] → 验证：[检查方式]
2. [步骤] → 验证：[检查方式]
3. [步骤] → 验证：[检查方式]
```

强成功标准让你能独立循环；弱标准（"让它能跑"）会不断需要人来澄清。

---

## 二、Git 与 Commit 规范

**未经用户明确要求，禁止执行 `git commit` / `git push`。**

**阶段任务完成时的例外流程**：当一个阶段性任务（PROMPTS.md 中定义的 P0/R1 等阶段）验收通过后，主动向用户汇报改动清单与测试结果，并**询问是否 commit / 打 tag**——询问后按用户指示执行，用户未答复前不提交。tag 节点与命名见 PROMPTS.md 的"提交与打 tag 节点"。

Commit message 使用 Conventional Commits（本仓库既有历史均遵循）：

```
<type>(<scope>): <简短描述，中文>

[可选正文：动机与影响面]
```

- type 取值：`feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `perf`
- scope 用模块名：`agent`、`context`、`providers`、`permissions`、`daemon`、`sandbox` 等
- 一个逻辑变更有规律地拆成多个 commit（如"实现 + 测试"可合为一个 feat commit），不要攒一个巨型 commit，也不要碎片化到每保存一次提交一次
- 描述写"为什么变了"比"改了哪个文件"更有价值

---

## 三、文件架构规范

### 目录职责（新代码必须放对地方）

```
src/flowcoder/
├── agent/            Agent 核心循环、事件模型、恢复快照（引擎心脏，改动需谨慎）
├── providers/        LLM 多协议适配与流式解析（不涉业务逻辑）
├── client/           LLM 客户端封装、错误分类
├── context/          上下文工程：压缩、卸载、恢复、预算
├── permissions/      权限门、路径沙箱、审批模式
├── sandbox/          Docker 沙箱：容器池（预热租借/销毁重建/回收）、限额、断网默认
├── eval/             HumanEval+ 评测流水线（Agent 的消费者，不改核心循环）
├── scheduler/        cron 调度器：定时驱动 Agent 回合、防抖/重试/P90 预触发
├── watchdog/         仓库看门狗：信号源、Agent 判定、四层防骚扰门控
├── tools/            内置工具实现
├── memory/           长期记忆与语义召回
├── teams/            多 Agent 协作
├── mcp/              MCP 客户端与工具接入
├── daemon/           Starlette 服务端、任务注册表、WebSocket
├── hooks/ skills/    生命周期钩子、声明式技能
├── config/           配置加载与校验
├── commands/         用户命令解析
├── ui/ gui/ tui 相关  展示层（不得反向依赖引擎层）
└── app.py            装配入口（已知 god file，禁止再往里加逻辑）
```

```
tests/
├── unit/             单元测试（不碰网络/文件系统，用 fake）
├── integration/      模块间集成测试
└── e2e/              端到端测试（稀缺资源，只在关键路径补）
```

新增顶层功能模块（如规划中的 `sandbox/`、`scheduler/`、`watchdog/`）时：单开子包、提供 `Protocol` 接口、在包内放 `__init__.py` 导出公共 API，并在 `docs/architecture/` 补一篇文档。

### 依赖方向（硬规则）

```
ui/gui/daemon  →  agent  →  { providers, context, permissions, memory, tools }
                              ↓
                          client / config（最底层，不依赖任何上层）
```

- 下层模块禁止 import 上层；展示层不得直接操作 LLM 客户端或权限门
- 跨模块通信优先走事件（agent 的事件流），不要互相直接调内部函数
- 引入新依赖前先看 `pyproject.toml`——本项目刻意极简，加依赖需要在 commit 正文里说明理由

---

## 四、文件拆分规范

**不看行数拆文件**。行数多不是问题，下面这些才是拆分信号：

1. **多个变化理由**：一个文件里的代码因为不同的需求会各自被修改（如"协议解析"和"重试策略"），拆开
2. **职责越界**：出现了与文件职责无关的辅助逻辑（如 daemon 里写 SSE 解析），移到对应模块
3. **测试定位困难**：改一处要理解整个文件才能写测试，拆
4. **依赖纠缠**：文件内一半函数只被外部一个调用方使用，考虑下沉或独立
5. **协议与实现混杂**：`Protocol` 接口和它的实现放一起时，如果实现有多个（如多 provider），接口独立成文件

反模式（不要做）：

- ❌ 按行数机械切分（"超过 500 行就拆"）
- ❌ 一个概念拆得过碎（一个类一个文件用到才组装）
- ❌ 为拆分而引入循环 import，再用局部 import 掩盖

**特别禁令**：禁止向 `app.py` 添加新逻辑。新装配代码放 `bootstrap` 方向的独立模块，有机会时逐步迁出（见 docs/plans/FRAMEWORK_REFACTOR_PLAN.md 阶段 3）。

---

## 五、代码质量规范

- **类型注解**：公共 API 必须标注；pyright 严格模式下零新增错误
- **异步纪律**：事件循环内禁止阻塞调用（同步文件 IO、`time.sleep`、同步 requests）；所有 IO 有超时；`asyncio.CancelledError` 必须放行，不得吞掉
- **错误处理**：只捕获预期中的具体异常；LLM 调用沿用 client 层的错误分类，不裸写 try/except
- **日志**：用 `logging`，不用 `print`；结构化字段带 `trace_id`；日志写"发生了什么+关键参数"，不写情绪化评论
- **注释**：只写代码本身说不出来的"为什么"（约束、取舍、坑），不复述代码在做什么
- **数据模型**：跨边界的数据用 pydantic v2 模型，不用裸 dict 传递
- **测试**：新功能必须带测试；测试用 fake 隔离外部依赖；pytest-asyncio auto 模式，测试文件与被测模块同名对应
- **提交前自检**：`ruff check` + `ruff format` + `pytest tests/unit`（涉及集成改动加跑 `tests/integration`）全部通过

---

## 六、文档阅读地图

开发前按任务类型读对应文档，**禁止在未读架构文档的情况下改核心模块**。

### 通用入口

1. `README.md` — 项目定位与形态
2. `docs/architecture/INDEX.md` → `overview.md` — 架构总览
3. `docs/architecture/data-flow-and-agent-loop.md` — 一次请求的完整数据流

### 按任务路由

| 要做的事 | 必读（按序） |
|---|---|
| 改 Agent 循环/事件模型 | `docs/architecture/agent-loop.md` → `docs/development/02-Agent核心运行循环.md` → `src/flowcoder/agent/core.py` |
| 改上下文/记忆 | `docs/architecture/context-memory.md` → `docs/development/10-上下文与记忆系统.md` |
| 改 LLM 客户端/流式协议 | `docs/architecture/llm-client.md` → `docs/development/03-LLM客户端与Provider适配层.md` |
| 新增/修改工具 | `docs/architecture/tools.md` → `docs/architecture/permissions-hooks.md`（工具必经权限门） |
| 改 daemon/任务调度 | `docs/architecture/daemon.md` → `docs/development/05-Daemon服务.md` |
| 改多 Agent/MCP | `docs/architecture/mcp-teams.md` → `docs/development/08-记忆技能MCP-Teams-A2A.md` |
| 改权限/钩子 | `docs/architecture/permissions-hooks.md` → `docs/development/07-权限Hook与上下文管理.md` |
| 改配置系统 | `docs/architecture/config.md` → `docs/development/04-配置系统.md` |
| 加可观测能力 | `docs/architecture/observability.md` |
| 新人上手/环境搭建 | `docs/development/getting-started.md` / `setup.md` / `contributing.md` |

### 规划类文档（做改造前必读）

- `docs/plans/TRANSFORMATION_PLAN.md` — 沙箱/评测/可靠性改造路线与当前阶段
- `docs/plans/FRAMEWORK_REFACTOR_PLAN.md` — Keel 框架抽象方案与依赖方向规则
- `docs/plans/PROJECT_REVIEW.md` — 项目现状快照与能力矩阵
- `docs/plans/DIFFERENTIATION_PLAN.md` — 后续差异化增量计划（A–D）：数据回填、注入防御、成本治理、可观测
- `CHANGELOG.md` — 已发布内容，写 release 前更新

### 写代码前的小仪式

1. 定位改动属于哪个模块 → 查上面的路由表
2. 读该模块架构文档 + 现有测试（测试是最好的用法示例）
3. 陈述假设与计划（含验证方式）→ 再动手
