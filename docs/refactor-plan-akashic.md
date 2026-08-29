# FlowCoder 借鉴 akashic-agent 改造计划

> 参考项目：`C:\Users\7\Desktop\星环Agent\akashic-agent-main`
> 改造目标项目：`D:\DEVELOP\python\FlowCoder`
> 文档版本：v1.0 · 2026-08-05

---

## 一、背景与改造目标

akashic-agent 的核心差异化在于它把 Agent 当作一个"有节奏、有记忆、有自驱能力的长期运行进程"来设计，而不是一次性问答器。经过对两个项目的逐文件比对，FlowCoder 的基础设施其实比表面看上去扎实得多——它已经具备独立的 Hook 引擎、多层上下文压缩熔断机制、可插拔记忆 provider 架构和工具前后钩子。因此本次改造不做"推倒重来"，而是聚焦 akashic 有而 FlowCoder 明显缺失、且能形成简历量化亮点的三块能力：**记忆检索流水线**、**记忆分层与 prompt cache 友好性**、**生命周期 phase 细化**。

改造遵循三条原则：第一，不破坏现有 `Agent.run()` 事件流契约，TUI/Daemon/GUI 三个消费方零感知；第二，每项改造必须可量化（召回率、token 占用、cache 命中率、延迟），能写进简历；第三，明确边界——akashic 的 Proactive 主动推送和 Drift 空闲任务与 FlowCoder"本地编码助手"定位偏离，本次不移植，仅作为架构认知储备。

---

## 二、现状盘点（精确到文件）

### 2.1 Agent 主循环与生命周期

主循环位于 [src/flowcoder/agent/core.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/core.py)，`Agent` 类持有 `client / registry / hook_engine / memory_hub / memory_bridge` 等依赖，主路径为"注入上下文 → 循环(压缩/pre_send/LLM/post_receive/工具执行) → 收敛结束"。生命周期钩子通过 [src/flowcoder/agent/hook_events.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/hook_events.py) 的 `run_lifecycle_hook()` 在固定位置触发，当前生命周期的切入点为 `session_start / turn_start / pre_send / post_receive` 四个点。底层 [src/flowcoder/hooks/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/hooks) 是一套独立的规则引擎（`engine.py / actions.py / conditions.py / executors.py / loader.py / models.py`），支持条件—动作型 hook，但**没有 phase 概念的显式抽象，也没有 slot 依赖声明**，hook 之间无法表达"我依赖另一个 hook 的输出"。

### 2.2 记忆与上下文系统

记忆侧有两条线。旧线是 [src/flowcoder/memory/auto_memory.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/memory/auto_memory.py) 的 `MemoryManager`，双层存储（用户级 `~/.flowcoder/memories.md` + 项目级 `<project>/.flowcoder/memories.md`），用 LLM 按 4 分类（用户偏好/纠正反馈/项目知识/参考资料）提取，每隔 `MEMORY_EXTRACTION_INTERVAL = 5` 轮触发一次（见 [core.py:133](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/core.py#L133)）。新线是 [src/flowcoder/memory/providers/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/memory/providers) 的 `MemoryHub` + `MarkdownMemoryProvider` 可插拔架构，由 `MEMORY_EVENT_TURN_COMPLETED` 事件驱动，[src/flowcoder/agent/memory.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/memory.py) 的 `AgentMemoryBridge` 做循环与 Hub 之间的适配。

上下文侧是 FlowCoder 的强项：[src/flowcoder/agent/compaction.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/compaction.py) 与 [src/flowcoder/context/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/context) 已实现 `auto_compact` + `CompactCircuitBreaker` 熔断器 + `ContentReplacementState`(Layer1 替换) + `RecoveryState`(Layer2 压缩后恢复近期读文件/skill 快照)，这套多层上下文管理比 akashic 文档里描述的还要细，**本次不动**。

明显短板有三：无向量检索（记忆只能全文注入，无法按 query 语义召回）；无 PENDING/MEMORY 分层（`memories.md` 单层结构，每次提取直接覆写全文，高频修改会击穿 prompt cache）；无检索增强组件（HyDE 改写、注入规划、充分性检查、去重）。

### 2.3 工具系统

工具侧 [src/flowcoder/agent/tool_hooks.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/tool_hooks.py) 已有 `run_pre_tool_hook / run_post_tool_hook` 前后钩子，[tool_authorization.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/tool_authorization.py) 有权限控制，[tool_execution.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/tool_execution.py) 有 `StreamingExecutor / ToolBatch` 批量执行与分区调度，[src/flowcoder/tools/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/tools) 下 `registry.py + base.py + impl/`（23 个内置工具）+ `agent/`（子 Agent 工具）+ `tasks/`。MCP 侧 [src/flowcoder/mcp/client.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/mcp/client.py) 文件存在但**未接入主循环**。工具 hook 是简单的 pre/post 两点，缺乏 akashic 那种植入 phase、可声明 slot 依赖的细粒度编排能力。

---

## 三、改造设计

三项改造按"价值/成本比"排序，建议从改造一开始推进，它增量最大、量化最直接。

### 改造一：记忆检索流水线（memory2 pipeline）

**目标**：把"全文注入 memories.md"升级为"按 query 语义召回 + 注入规划 + 充分性校验"的工业级检索流水线，对齐 akashic [memory2/](file:///c:/Users/7/Desktop/%E6%98%9F%E7%8E%AFAgent/akashic-agent-main/memory2) 的设计，并在 FlowCoder 里做到可量化。

**设计**：在 [src/flowcoder/memory/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/memory) 下新增 `retrieval/` 子包，实现一条可插拔的检索流水线，复用现有 `MemoryHub` provider 架构，不另起炉灶。流水线分五段，每段是一个独立的异步阶段，输入输出均为 dataclass，便于单测与替换：

第一段是 `query_rewriter`，用 fast 模型（[config.yaml](file:///D:/DEVELOP/python/FlowCoder) 里已配置的 `qwen-flash` 对应轻量模型）把用户原句改写为检索友好的形式，处理指代消解与多意图拆分。第二段是 `hyde_enhancer`，基于改写后的 query 用 LLM 生成一段"假设答案文档"，再用该文档的 embedding 做检索，提升语义召回（HyDE 思路）。第三段是 `retriever`，对接向量层——向量存储复用项目已有的 embedding provider，新增 `memory2.db`（SQLite + sqlite-vec 或 chromadb，优先选无服务依赖的嵌入式方案）。第四段是 `injection_planner`，根据当前上下文剩余 token 预算与命中条目的相关性分数，决定注入几条、以什么顺序、是否需要再触发一轮检索。第五段是 `sufficiency_checker`，在注入后评估"已注入记忆是否足以回答当前 query"，不足则回到第一段做扩检，设置最大扩检轮数（默认 2）防发散。额外加一个 `dedup_decider` 在注入前做去重，避免同义记忆重复占用 token。

**落地文件清单**：新增 `src/flowcoder/memory/retrieval/{__init__.py, pipeline.py, query_rewriter.py, hyde_enhancer.py, retriever.py, injection_planner.py, sufficiency_checker.py, dedup_decider.py, store.py, models.py}`；新增 `src/flowcoder/memory/retrieval/store.py` 封装向量库；在 [src/flowcoder/agent/memory.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/memory.py) 的 `AgentMemoryBridge` 中增加 `recall(query)` 方法，由主循环在 `pre_send` 之前调用；在 [src/flowcoder/agent/llm_preparation.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/llm_preparation.py) 的 `prepare_api_conversation` 中把召回结果作为独立 system block 注入，**放在稳定前缀之后、对话历史之前**，以兼顾 cache 命中与时效性。配置侧在 `config.yaml` 新增 `[memory.retrieval]` 段，开关 `enabled`、`top_k`、`max_expand_rounds`、`injection_token_budget`。

**量化指标**（写进简历的关键）：构建一个 50 条带标注的 query—记忆对评测集（放在 `eval/memory_recall/`），对比改造前（全文注入）与改造后（检索流水线）的 Recall@5、MRR、单轮注入 token 占用（目标从 ~2000 token 降到 ~500 token）、端到端延迟增量（目标 < 400ms，fast 模型异步并行）。同时统计长会话（20 轮+）下的命中率与扩检触发率。

**风险与缓解**：HyDE 与改写都调用 fast 模型会增加延迟，缓解方式是 query_rewriter 与 hyde_enhancer 并行执行、结果取并集，并对短 query（< 8 字）跳过 HyDE 直接检索。向量库选型上优先 sqlite-vec（纯 Python 无服务依赖，符合 FlowCoder local-first 定位），若遇性能瓶颈再换 chromadb。

### 改造二：记忆分层与 prompt cache 友好性

**目标**：对齐 akashic 的 consolidation 分层思路，把单层 `memories.md` 升级为 `PENDING.md → MEMORY.md` 两层归档，并在 system prompt 拼接顺序上显式为 prompt cache 优化，让 prefix 在多轮间保持稳定。

**设计**：akashic 的核心洞察是"MEMORY.md 全文注入 system prompt，高频修改会破坏 prompt cache，所以高频写 PENDING、低频归档 MEMORY"。FlowCoder 现有 [auto_memory.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/memory/auto_memory.py) 每次提取直接覆写 `memories.md` 全文，正好踩中这个坑。改造方案是引入三文件结构：`RECENT_CONTEXT.md`（近期上下文摘要，每轮轻量更新）+ `PENDING.md`（待归档缓冲，每轮提取写入，**不进 system prompt**）+ `MEMORY.md`（稳定归档，全文注入 system prompt 前缀）。新增 `Optimizer` 任务，按"距离上次归档轮数 ≥ 10 或 PENDING 条目 ≥ 20"触发，把 PENDING 合并去重后写入 MEMORY，并清空 PENDING。这样 system prompt 里的 MEMORY.md 在 10 轮内保持字节级稳定，prefix cache 命中率最大化。

prompt 拼接顺序在 [src/flowcoder/prompts/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/prompts) 的 `build_system_prompt` 中调整为：`[环境上下文(稳定)] → [MEMORY.md(低频变)] → [技能清单(中频变)] → [召回记忆(每轮变，放靠后)]`，把高频变动内容尽量后移，让前缀稳定区最大化。Anthropic 协议下显式设置 `cache_control` breakpoint 在 MEMORY.md 之后。

**落地文件清单**：改造 [src/flowcoder/memory/auto_memory.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/memory/auto_memory.py) 增加三文件读写与 `Optimizer`；在 [src/flowcoder/memory/providers/](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/memory/providers) 新增 `LayeredMarkdownProvider` 替换现有 `MarkdownMemoryProvider`（保留旧 provider 做回退）；在 `build_system_prompt` 调整拼接顺序与 cache breakpoint。

**量化指标**：用 Anthropic API 返回的 `cache_creation_input_tokens / cache_read_input_tokens` 统计改造前后 20 轮会话的 cache 命中率（目标 cache_read 占比从 ~30% 提升到 ~70%），并换算 token 成本节省。同时记录 PENDING→MEMORY 归档触发频率与归档耗时。

**风险与缓解**：分层后用户可能想直接看全部记忆，缓解方式是提供 `flowcoder memory show` 命令合并三文件展示；旧 `memories.md` 需一次性迁移脚本升级为 MEMORY.md，迁移逻辑放在 `Optimizer` 首次运行时检测旧文件自动转换。

### 改造三：生命周期 phase 细化与 slot 依赖

**目标**：把现有 4 个生命周期点（`session_start / turn_start / pre_send / post_receive`）细化为对齐 akashic 的 7-Phase 模型，并给 hook 增加 slot 依赖声明能力，让插件能精确表达"在 prompt 渲染前注入记忆""在 reasoning 后做安全审计"这类时机需求。

**设计**：参考 akashic [agent/lifecycle/phases/](file:///c:/Users/7/Desktop/%E6%98%9F%E7%8E%AFAgent/akashic-agent-main/agent/lifecycle/phases) 的 7 个 Frame，在 FlowCoder 里落地为 `BeforeTurn → BeforeStep → BeforeReasoning → PromptRender → AfterReasoning → AfterStep → AfterTurn`，区分 turn 级与 step 级两个粒度（一次 turn 可含多步工具调用）。每个 phase 是一个 dataclass Frame，持有入参/出参 slot。现有 `HookEngine` 已有 actions/conditions/executors，本次在其上增加 slot 依赖拓扑：hook 声明 `requires=["memory.query"] / provides=["memory.injection"]`，引擎在执行 phase 前做拓扑排序，缺依赖则跳过该 hook 并打 warning。这套设计让改造一的检索流水线、改造二的记忆注入都能作为 phase hook 接入，而不是硬编码进主循环。

主循环 [core.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/core.py) 的改造保持克制：现有 `run_lifecycle_hook(event=...)` 调用点不动，只是把 `event` 字符串扩展为 phase 枚举，并在每个 phase 前后插入 Frame 边界事件。事件流契约不变，TUI/Daemon/GUI 零感知。

**落地文件清单**：新增 `src/flowcoder/agent/lifecycle/{__init__.py, phases.py, frame.py, slot_graph.py}`；改造 [src/flowcoder/hooks/engine.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/hooks/engine.py) 增加 slot 拓扑排序；改造 [src/flowcoder/agent/hook_events.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/hook_events.py) 扩展 phase 枚举；在 [core.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/agent/core.py) 主循环的对应位置插入 Frame 边界。

**量化指标**：phase 数量（4 → 7）、可挂载 hook 点数量、slot 依赖声明覆盖率（接入的 hook 中声明 requires/provides 的比例）。架构层面的改进较难直接量化，配套写一份 `docs/lifecycle-design.md` 把设计权衡讲清楚，作为面试讲解材料。

**风险与缓解**：phase 细化会让主循环调用点变多，需防止 hook 执行串行化导致延迟上升，缓解方式是同一 phase 内无 slot 依赖的 hook 并行执行（`asyncio.gather`），并给每个 phase 加超时熔断（复用现有 `CompactCircuitBreaker` 模式）。

---

## 四、明确不做的事项（边界）

akashic 的 Proactive 主动推送（电量模型自适应轮询）与 Drift 空闲任务（SKILL.md 驱动自驱）虽然惊艳，但前者依赖长期常驻进程与外部数据源订阅，后者依赖"Agent 闲暇时间"语义，均与 FlowCoder"按需启动的本地编码助手"定位冲突，强行移植会引入大量闲置复杂度，本次明确不做。MCP 集成（[mcp/client.py](file:///D:/DEVELOP/python/FlowCoder/src/flowcoder/mcp/client.py) 已存在但未接入主循环）属于工具扩展层，与三项改造正交，留作后续独立任务。上下文压缩（compaction）FlowCoder 已比 akashic 更细，明确不动。多 Agent 协作（teams/a2a）不在本次范围。

---

## 五、排期建议

三项改造存在依赖关系：改造三（phase 细化）为改造一、二提供了接入点，但改造一、二也可以先用现有 `pre_send` 点接入、后续再迁到 phase。建议排期如下，可按实际可用时间压缩或并行：

第一周完成改造一（记忆检索流水线），重点是 `retrieval/` 子包 + 评测集 + 量化报告，这是简历最大亮点，应优先拿下来。第二周前半完成改造二（记忆分层 + prompt cache），后半启动改造三（phase 细化）。第三周完成改造三并做整体回归，把三块改造的量化数据汇总成一份 `docs/refactor-report.md` 作为面试材料。若时间紧张，改造三可降级为"仅扩展 phase 枚举、暂不实现 slot 拓扑"，保住改造一、二两块硬量化成果。

---

## 六、总体风险与缓解

最大风险是改造破坏现有 `Agent.run()` 事件流契约，影响 TUI/Daemon/GUI 三端。缓解方式是每项改造都先用开关控制（`config.yaml` 里 `enabled` 默认 false），改造一、二的开关在 memory 段，改造三的开关在 lifecycle 段，开关关闭时行为与改造前完全一致，便于随时回退。第二风险是向量库与 fast 模型调用引入新的运行时依赖，缓解方式是向量库选 sqlite-vec 纯 Python 方案、fast 模型调用复用现有 provider 抽象，并在 `requirements.txt` 里隔离可选依赖。第三风险是评测集质量决定量化数字可信度，缓解方式是评测集 50 条 query—记忆对人工标注，覆盖指代、多意图、长尾三类场景，评测脚本放 `eval/memory_recall/` 可复现。

---

## 七、验收标准

每项改造需同时满足：开关关闭时全量测试通过（回归零退化）、开关打开时新增模块单测覆盖率达到 80%+、量化指标达标（改造一 Recall@5 提升 ≥ 0.15 且注入 token 降幅 ≥ 60%、改造二 cache 命中率提升 ≥ 30 个百分点、改造三 phase 数 = 7 且 slot 声明覆盖率 ≥ 60%）、`docs/` 下有对应设计文档与量化报告。三项全部完成后，整体改造作为"FlowCoder 深度二次开发"的核心技术亮点写入简历，配合 `docs/refactor-report.md` 作为面试讲解依据。
