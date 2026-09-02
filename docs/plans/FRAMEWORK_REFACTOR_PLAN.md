# Keel 框架抽象重构方案

> **执行计划**：本文件是总方案（为什么/差异化/路线）；落到真实代码的逐步执行计划
> （文件映射、端口签名映射、验收标准、护栏代码）见
> `docs/specs/2026-08-29-keel-r1-r4-detailed-plan.md`（2026-08-29，基于当时代码实况勘定）。

> 目标：把 FlowCoder 的"机制"从"编码场景策略"中剥离，抽象成通用 Agent 框架 **Keel（龙骨）**。
> 定位：不是又一个 LangGraph / OpenAI Agents SDK 复制品——差异点见第三节。
> 时机：在 TRANSFORMATION_PLAN 的 Phase 1–2 完成之后再启动（先有增量贡献，再做抽象；且沙箱/评测本身要成为框架的可选模块）。

## 一、主流框架盘点与空白

| 框架 | 核心抽象 | 明显的空白 |
|---|---|---|
| LangGraph | 图编排 + checkpoint | 编排重、上下文管理 DIY、权限是用户代码的事 |
| OpenAI Agents SDK | 轻量循环 + handoff + tracing | 无上下文治理、无预算约束、安全靠自觉 |
| CrewAI | 角色化多 Agent | 工程治理几乎为零，适合 demo 不适合生产 |
| PydanticAI | 类型安全的工具/结构化输出 | 同上，运行时治理缺位 |
| Claude Agent SDK | 产品级 harness | 闭源生态绑定，不可自建 |

共识结论：**主流框架都在解决"怎么编排"，没有一家把"运行时治理"（上下文预算、权限、资源纪律、执行隔离）做成一等公民**。这就是 Keel 的立足点。

## 二、分层架构

```
keel/                      ← 框架层（纯机制，无领域知识）
├── engine/                Agent 循环：事件溯源式异步生成器，max_iterations、收敛判定
├── events/                统一事件协议（StreamText/ToolUseEvent/PermissionRequest/...）
├── context/               上下文治理器：预算声明、压缩、卸载、恢复
├── providers/             多协议 LLM 适配（流式解析）+ resilience（限流/退避/故障转移）
├── policy/                权限门：能力模型 + 可组合策略 + 审批挂起/恢复
├── budget/                预算纪律：token/时间/步数/成本，引擎强制执行
├── sandbox/               沙箱执行池（可选模块，依赖 docker）
├── memory/                MemoryHub 可插拔记忆接口
├── observe/               事件日志 + TraceManager（每次运行可完整回放）
└── ext/                   MCP 客户端、HookEngine、多 Agent 通信

profiles/                  ← 应用层（策略，领域专属）
├── coder/                 编码 Agent：bash/edit 工具、代码 prompt、skills
├── office/（预留）        办公 Agent：文档工具集
└── minimal/               验证用最小 profile
```

核心接口（五条端口，全部面向协议而非实现）：

```python
class Provider(Protocol):    # LLM 调用（流式事件输出）
    def stream(self, req) -> AsyncIterator[LLMEvent]: ...
class Tool(Protocol):        # 工具：声明式 schema + 执行
    async def execute(self, args) -> ToolResult: ...
class Policy(Protocol):      # 权限策略：可组合，允许/拒绝/挂起等审批
    def check(self, call: ToolCall) -> Decision: ...
class Memory(Protocol):      # 记忆读写
class Sandbox(Protocol):     # 执行环境抽象（Docker 实现之外可换 subprocess/E2B）
```

## 三、特色功能（与主流的区分点，每个都可深挖）

**1. 事件溯源循环 —— "Agent 运行的 git"**
每次运行的每个决策（上下文注入、LLM 输出、工具调用、权限判定）都是 append-only 事件。由此免费获得：任意步 rewind、从任意事件 fork 出平行分支探索、确定性回放调试。LangGraph 的 checkpoint 是状态快照，Keel 是可回放的事件日志 + fork UX——定位是"调试体验"，这是主流框架都没做好的。

**2. 上下文预算治理（Context Governor）—— 把上下文当受管资源**
用户声明预算：`system: 2k / tools: 1k / history: 30k / tool_results: 8k`，治理器负责压缩、卸载、对齐，并提供内省 API（"这次运行的 token 花在哪了"）。主流框架把上下文管理完全留给用户——这是 Agent 生产事故的最大来源，Keel 把它做成引擎职责。FlowCoder 的 auto_compact/断路器直接下沉为第一个实现。

**3. 能力即策略（Capability-based Policy）**
权限不是工具上的一个 callback，而是循环内的一等公民：策略可组合（路径沙箱 ∧ 命令规则 ∧ 人工审批），判定为"挂起"时循环安全停驻、审批后从断点续跑。FlowCoder 的四模式审批 + 沙箱池在此汇合成完整安全层。

**4. 预算纪律（Budget Discipline）**
token/时间/步数/成本四维预算由**引擎确定性执行**（超限触发总结收敛/升级人工），而非 prompt 恳求。主流框架全缺。

**5. 沙箱执行一等公民**
`sandbox/` 是框架可选模块而非外部设施——`Tool` 可声明 `requires="sandbox"`，引擎自动路由进沙箱池执行。目前没有任何主流框架内置执行隔离。

## 四、重构路线（绞杀者模式，四阶段，全程测试绿）

**阶段 1：事件协议先行（1 周）**
把 `agent/core.py` 产出的事件类型抽到 `keel/events/`，FlowCoder 改为从框架 import。不改行为，只动归属。

**阶段 2：五端口抽取（2 周）**
循环依赖倒置：`keel/engine` 只认 Provider/Tool/Policy/Memory/Sandbox 五个协议；providers、permissions、memory、sandbox 逐一迁入框架并实现端口。每迁一个模块跑全量测试。

**阶段 3：策略剥离（1–2 周）**
编码专属物（bash/edit 工具、编码 prompt、skills、TUI）迁入 `profiles/coder/`；FlowCoder 变成"Keel + coder profile"的组装产物。顺手拆掉 `app.py` god file。

**阶段 4：抽象验证（1 周）——最关键**
写 `profiles/minimal/`：一个只依赖框架的数据问答 Agent（CSV 问答即可）。**验收标准：不改动 keel/ 一行代码就能跑通。**跑不通说明抽象泄漏，回炉。

护栏（借鉴 flow-agent 的架构边界测试）：`tests/architecture/` 下用 import-graph 测试强制约束依赖方向——profiles 只准 import keel，keel 禁止 import 任何 profile；这条规则用 CI 执行，不靠自觉。

## 五、顺序与风险

- **总顺序**：Phase 0–1（沙箱）→ Phase 2（评测）→ 重构四阶段 → Phase 3–4（可靠性/混沌，落在框架层收益最大）
- **最大风险**：抽象歪了导致框架和 profile 互相拉扯。对策：阶段 4 的 minimal profile 是唯一裁判，跑不通就退回；任何一轮重构不超过两周不合并
- **简历表述**："基于开源编码 Agent 运行时二次开发，将机制层抽取为通用 Agent 框架 Keel（事件溯源循环 / 上下文预算治理 / 能力即策略 / 引擎级预算纪律 / 内置沙箱），并以第二个 profile 验证抽象"——诚实且每一条都是自己的工作
