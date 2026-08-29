# 上下文与记忆系统走读笔记（context/ 包）

> P0 阶段产出（2026-08-29）。基线：main @ 6e371d5。
> 每个结论后附源码行号，可逐条核对。

## 一、模块结构

`context/` 是四层结构，每层解决一个不同粒度的问题：

| 文件 | 职责 | 关键入口 |
|---|---|---|
| `tool_results.py`（315 行） | **Layer1**：单次 API 调用的 tool-result 预算与落盘 | `apply_tool_result_budget` |
| `manager.py`（412 行） | **Layer2**：全对话摘要压缩 + 熔断器 + 阈值 | `auto_compact` |
| `replacement.py`（100 行） | 替换决策的持久化状态（支撑 Layer1 跨轮一致性） | `ContentReplacementState` |
| `recovery.py`（185 行） | 压缩后的工作集恢复附件（支撑 Layer2） | `build_recovery_attachment` |
| `../agent/llm_preparation.py` | Agent 侧的 Layer1 编排入口 | `prepare_api_conversation` |
| `../agent/compaction.py` | Agent 侧的注入与压缩后重建 | `inject_agent_context` / `reinject_after_compact` |

两层的设计动机（这是整个包最重要的决策）：

- **Layer1 是无损失的、每轮的**：原始 history 永远保留完整内容，只对"这一帧
  发给 API 的副本"做预算裁剪。tool_results.py:102-115 的 docstring 明确写了
  "Design B: 不 mutate 原 conversation"。
- **Layer2 是有损失的、极少触发的**：只有接近窗口上限才重写 history 本身，
  用摘要换空间。

## 二、Layer1：单次调用的 tool-result 预算

调用链：`prepare_api_conversation`（llm_preparation.py:46-58）→
`apply_tool_result_budget`（tool_results.py:102-200）→ 返回新 ConversationManager。

### 预算常量（tool_results.py:22-28）

| 常量 | 值 | 含义 |
|---|---|---|
| `SINGLE_RESULT_CHAR_LIMIT` | 50,000 | 单条结果超限 → 落盘 |
| `INLINE_RESULT_CHAR_LIMIT` | 10,000 | 单条超限 → 原地截断 |
| `AGGREGATE_CHAR_LIMIT` | 200,000 | 聚合总量超限 → 逐条落盘直到达标 |
| `PREVIEW_CHARS` | 2,000 | 落盘预览长度 |
| `KEEP_RECENT_TURNS` | 10 | Pass3 保留的近期轮数 |
| `OLD_RESULT_SNIP_CHARS` | 2,000 | Pass3 裁剪阈值 |

### 三个 Pass 的顺序与理由（tool_results.py:144-190）

1. **Pass 1（单条超限，144-155）**：fresh 结果里超过 50K 字符的直接落盘为
   `<persisted-output>` 预览（写 `session_dir/{tool_use_id}.txt`，tool_results.py:66-74）。
2. **Pass 2（聚合超限，157-181）**：剩余内容总量超过 200K 时，按大小降序逐条
   落盘直到达标。为什么按大小降序：落盘一条最大的就能回收最多空间，改动条数最少。
3. **Pass 3（陈旧裁剪，`_snip_stale_messages`，271-315）**：最近 10 轮之外的
   tool_result，超过 2K 字符的压成 200 字符预览加 `<snipped>` 标记。docstring
   明确承认这是 stateless、边界会 drift 的已知 trade-off（tool_results.py:111-112）。

### 为什么需要一个跨轮状态（replacement.py 的存在理由）

每轮都重新构建 api_conv，如果"这条结果要不要替换"的决策每轮重算，会出现
两个问题：重复落盘（有 O_EXCL 保护，tool_results.py:69-73）、决策不一致。
所以引入 `ContentReplacementState`（replacement.py:15-19）：

- `seen_ids`：见过的 tool_use_id（未替换的也记，tool_results.py:183-186），
  表示"这条的决策已做过"；
- `replacements`：tool_use_id → 替换后的内容，之后每轮直接查表复用
  （tool_results.py:129-132）。

决策同时追加写入 `replacement_records.jsonl`（replacement.py:42-58），
会话恢复时 `reconstruct_replacement_state` 用它重建状态
（replacement.py:82-100）——只对当前 history 中真实存在的 tool_use_id 恢复，
防止记录与历史错位。

### 与工具产出侧的配合

工具结果在入对话前就做了第一道预算：`prepare_tool_result_content`
（tool_results.py:91-99）——超过 50K 落盘+预览（`<persisted-output>` 标签），
超过 10K 截断。Layer1 看到 `PERSISTED_TAG` 开头的结果会直接采纳为替换决策
（tool_results.py:133-140），不再重复判断。

## 三、Layer2：全对话摘要压缩

### 触发条件与阈值（manager.py:68-82,116-123）

```
threshold = context_window - SUMMARY_OUTPUT_RESERVE(20_000) - margin
auto: margin = 13_000    manual: margin = 3_000
```

- 20K 预留：给摘要生成本身留输出空间（manager.py:68）。
- auto 的 margin 更大：自动压缩触发时对话还在增长，需要更多缓冲；手动
  `/compact` 是用户明确的动作，可以直接压到贴近上限。
- 阈值判断的锚点：`conversation.current_tokens()` = 上次真实计费
  （input + cache_read + cache_creation + output）+ 之后新增消息的字符估算；
  冷启动/刚压缩后退化为全量估算（manager.py:303-306）。为什么用真实用量锚定：
  纯字符估算对 cache 命中的对话偏差很大。

### 保留窗口的三个约束（manager.py:194-232）

从尾部向前累计，满足**任一**保底条件即停：

- 累计 ≥ `KEEP_RECENT_TOKENS`(10K)；
- 条数 ≥ `MIN_KEEP_MESSAGES`(5)；
- 但累计一旦会超过 `KEEP_MAX_TOKENS`(40K) 就立即停（manager.py:220-221）——
  防止单条超大尾部消息把整个窗口吃掉；最后一条消息无论如何都保留。

### tool_use / tool_result 配对对齐（manager.py:235-251）

保留窗口的起点若落在携带 tool_results 的 user 消息上，就向前回退到配对的
assistant tool_use 消息。为什么：协议要求 tool_result 必须紧跟对应的 tool_use，
保留半对会产生 API 400 或"模型无法归属的悬空结果"。宁可多保留一对。

### 值不值得压（manager.py:254-258,82）

待摘要前缀 < `MIN_SUMMARIZE_PREFIX_TOKENS`(2K) 时直接返回 None 不压——
摘要往返的 token 开销比回收的空间还大，避免"压了个寂寞"。

### 摘要生成（manager.py:126-153,327-383）

- 固定 9 段结构化摘要 prompt（主要请求/技术概念/文件代码/错误修复/解决过程/
  **所有用户消息原文**/待办/当前工作/下一步），严禁调用工具（manager.py:126-145）。
  用户消息要求原文保留不可改写——这是恢复执行时最重要的锚点。
- 输出里 `<analysis>` 部分被丢弃、只取 `<summary>`（manager.py:148-153）：
  让模型先思考再总结，但思考内容不占压缩后的空间。
- 失败重试 3 次（manager.py:348-375）；错误消息含 "prompt long"/"too many" 时
  按轮分组丢弃最旧的 1/5 再试（manager.py:367-374）——摘要用的对话超窗时
  自我降级。注意 manager.py:366 的条件组合：`"prompt" in err and "long" in err
  or "too many" in err`，运算符优先级使 "too many" 单独成支——这是 P0.5
  审计项（context/manager.py:353），修复方向是加括号明确原意。

### 熔断器（manager.py:266-278,311-312）

连续 3 次摘要失败 → 熔断打开，auto_compact 直接返回错误字符串，不再尝试。
为什么：摘要失败通常意味着系统性问题（网络/超窗/配置），反复重试只会烧钱
且每次失败还要重新累计——熔断把决定权交还给用户（手动 compact 或 daemon API）。

### 重建与锚点清零（manager.py:386-412）

压缩后 history = [摘要 user 消息（含恢复附件 + transcript 指引）] + [保留尾部]。
`replace_history` 会清零用量锚点（manager.py:396-401 注释解释了为什么必须清零：
旧 anchor 对应压缩前的消息列表，不清零会让 current_tokens() 的增量估算错乱），
下一次 API 响应重新锚定。同时 `cleanup_tool_results` 清空落盘目录
（manager.py:402）——旧落盘内容已被摘要取代，留着只会误导恢复。

### 恢复附件（recovery.py:113-185）

摘要是有损的，压缩会把"工作集"抹掉。`build_recovery_attachment` 在摘要消息里
拼回三段：

1. **最近读过的文件**（最多 5 个，每个截到 5K token，recovery.py:15-16）——
   并明确标注"如需当前字节请重新读取"（recovery.py:124）；
2. **已激活的技能**（总预算 25K，单技能 5K，recovery.py:17-18）——skill 的
   SOP 不因压缩失效；
3. **可用工具清单**（名称 + 首行描述，recovery.py:157-176）。

为什么这三样：模型压缩后最容易丢的就是"我在改什么文件/我在按什么流程做/
我还能用什么工具"。字符转 token 用 3.5 chars/token 估算（recovery.py:19），
与全项目估算口径一致。最后附统一提示"需要原文就用工具重读，不要按摘要猜"
（recovery.py:181-184）。

## 四、与 Agent 循环的衔接点

| 衔接 | 位置 | 说明 |
|---|---|---|
| Layer1 调用 | agent/core.py:427（`prepare_api_conversation`） | 紧贴 LLM 调用，每轮 |
| Layer2 触发 | agent/core.py:321-344（`should_auto_compact` → `auto_compact`） | pre_send 前，每轮检查 |
| 压缩后重注入 | agent/core.py:346-362（`reinject_after_compact`） | 环境上下文/指令/记忆不参与摘要，需显式拼回 |
| 手动压缩 | agent/core.py:797-832（`manual_compact`） | `manual=True`，margin 缩到 3K，绕过熔断检查 |
| 状态归属 | `Agent.__init__`（core.py:156-161） | breaker / replacement_state / recovery_state 均 per-agent |
| 会话持久化 | replacement_records.jsonl + transcript JSONL | 恢复时重建替换状态（replacement.py:82-100） |

## 五、疑点登记（后续阶段处理）

1. manager.py:366 的条件表达式优先级歧义——P0.5 修复项，见上。
2. `apply_tool_result_budget` 的 Pass3 边界 drift 是已声明的 trade-off
   （tool_results.py:111-112），若未来出现"恢复后重复裁剪/漏裁剪"问题，这里是
   第一排查点。
3. `_group_messages_by_turn` 只在摘要重试降级路径使用，把"无 tool_use 的
   assistant 消息"当轮边界（manager.py:176-186）——若对话以连续 tool_use
   收尾，分组可能不均匀，影响降级丢弃的粒度，仅登记。
