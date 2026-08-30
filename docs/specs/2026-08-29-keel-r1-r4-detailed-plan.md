# Keel 框架重构详细计划（R1–R4 执行版）

- 状态：Accepted（待逐阶段执行）
- 日期：2026-08-29
- 前置：`FRAMEWORK_REFACTOR_PLAN.md`（总方案与差异化定位）+ 本文（落到真实代码的执行计划）
- 铁律：**任何一行提交都必须能回答"这行属于哪一步、验收标准是哪条"**

## 一、目标与三条不变量

| # | 不变量 | 验证方式 |
|---|---|---|
| I1 | 行为零变化 | 全量测试（1607+）每阶段结束全绿；用户可见行为（TUI/daemon/CLI）不变 |
| I2 | 每阶段独立可合并 | 每阶段 ≤1 周、单独 commit 序列、单独 merge main；失败可独立回滚到上一 tag |
| I3 | 依赖方向由 CI 强制 | `tests/architecture/` 的 import-graph 测试（见第四节），不靠自觉 |

"标准、可读"在本计划的落地：keel 包结构与主流框架惯例对齐（engine/events/policy 分层）、
每个模块 docstring 说明"是什么/不是什么"、公共 API 只从包 `__init__` 导出、
**禁止兼容 shim 存活超过一个阶段**（导出路径迁移完毕即删，防止双路径腐化）。

## 二、现状盘点（2026-08-29，映射的真实锚点）

### 2.1 五端口现状

| Keel 端口 | 现有实现 | 位置 | 抽取难度 |
|---|---|---|---|
| Provider | `LLMClient`（抽象）+ 三协议实现 + `create_client` 韧性包裹 | `client/base.py` / `client/core.py` / `client/factory.py` | 低——P5 已拆好 base/factory |
| Tool | `Tool` ABC（name/description/params_model/execute + defer/concurrency 元数据） | `tools/base.py:24` | 低——接口已标准 |
| Policy | `PermissionChecker`（检测器+沙箱+规则引擎+模式矩阵） | `permissions/checker.py:29` | **中**——是具体类非协议，需先提炼端口再迁实现 |
| Memory | `MemoryHub`（provider 插拔） | `memory/providers/hub.py:40` | 中——hub 具体类，Memory 端口从其公开方法提炼 |
| Sandbox | `ContainerRuntime` Protocol + `SandboxPool` | `sandbox/runtime.py:24` / `pool.py` | **零**——P1a 起就是 Protocol，直接搬家 |

### 2.2 事件协议现状（R1 范围）

`agent/events.py`：15 个事件 dataclass + `PermissionResponse` 枚举
（StreamText / ThinkingText / RetryEvent / ToolUseEvent / ToolResultEvent / TurnComplete /
LoopComplete / UsageEvent / ErrorEvent / CompactNotification / CompactStarted /
HookEvent / PermissionRequest / AskUserRequest / PermissionResponse）。

**消费面 17 个文件**（agent 包外）：app.py、daemon/×7、eval/×2、skills、teams、tools/×4、ui。
→ R1 的核心工作量就是这 17 个文件的 import 改写。

### 2.3 app.py 结构（R3 拆分映射，2057 行）

| 行段 | 内容 | 去向（R3） |
|---|---|---|
| 115–149 | `@` 文件引用展开（scan_files_for_at / expand_at_refs） | `profiles/coder/at_refs.py` |
| 150–316 | `ChatInput`（TextArea 子类，命令补全/历史/@事件） | `profiles/coder/tui/input.py` |
| 318–557 | 工具块渲染（`_tool_title`/`_format_detail`/`ToolCallBlock`/`_to_past_tense`） | `profiles/coder/tui/tool_blocks.py` |
| 560–583 | `ToolGroupSummary` | 同上 |
| 584–658 | `SubAgentBlock` | `profiles/coder/tui/subagent_block.py` |
| 659–701 | `SerializedSendGate`（发送串行化） | `keel/engine/send_gate.py`（机制，非编码专属） |
| 702–2057 | `FlowCoderApp`（1355 行：装配/事件消费/对话框/命令/状态） | 拆两半：通用壳 → `profiles/coder/tui/app.py`；纯机制（send gate 之外的预算/事件消费模式）下沉 keel |

### 2.4 已就绪、直接受益的机制资产（keel 的差异化卖点，均有 ADR）

韧性层（client/resilience.py）、预算闸（agent/budget.py）、Outbox（daemon/outbox.py）、
调度器（scheduler/）、看门狗（watchdog/）、沙箱池（sandbox/）。

## 三、目标架构（细化自 FRAMEWORK_REFACTOR_PLAN 第二节）

```
src/keel/                    ← 与 flowcoder 同级的 src-layout 常规包（ADR-R1-D1）
├── __init__.py              版本 + 公共 API 导出（唯一的稳定 import 面）
├── events/                  R1：事件协议（15 事件原样搬迁，见 §5.2）
├── engine/                  R2：主循环骨架 + 发送串行化 + 预算闸挂载点
│   ├── loop.py              （agent/core.py 的编排骨架，策略经端口注入）
│   ├── budget.py            （← agent/budget.py）
│   └── send_gate.py         （← app.py SerializedSendGate）
├── providers/               R2：Provider 端口 + 三协议实现 + 韧性层
│   ├── base.py / core.py / factory.py / resilience.py（← client/ 原样升迁）
│   └── errors.py / error_mapping.py / context_window.py
├── policy/                  R2：Policy 端口 + PermissionChecker 实现
├── memory/                  R2：Memory 端口 + MemoryHub 实现
├── sandbox/                 R2：Sandbox 端口 + 容器池（← sandbox/ 原样升迁）
├── context/                 R2：上下文治理（compaction/tool_results/manager）
└── observe/                 R2：TraceManager（← agents/trace.py）

src/flowcoder/               ← "Keel + coder profile" 的组装产物（R3 后）
├── profiles 实际落位为 flowcoder 内的 coder 模块集（tools/ prompts/ tui/）
└── daemon/ scheduler/ watchdog/ eval/  保留（daemon 是运行形态，eval 是消费者）
```

两个需要 ADR 记录的命名决策：
- **D-位置**：`src/keel`（与 flowcoder 同级、同一 src-layout）而非独立顶层 `keel/`——共用
  pyproject/测试/CI 基建，hatch build targets 加一行即可。
- **D-迁移 vs 复制**：模块**移动**（git mv 语义）而非复制——防止双实现漂移；
  旧路径留 shim re-export 一个阶段后删除。

## 四、架构护栏（R1 就位，全程生效）

`tests/architecture/test_dependencies.py`（导入图测试，随 R1 首个 commit 建立）：

```python
"""架构边界：keel 禁止 import flowcoder；flowcoder 必须经 keel 取事件（R1 后）。"""
import ast
from pathlib import Path

KEEL_ROOT = Path("src/keel")
FORBIDDEN = {"flowcoder"}

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out

def test_keel_never_imports_flowcoder():
    for path in KEEL_ROOT.rglob("*.py"):
        bad = _imports(path) & FORBIDDEN
        assert not bad, f"{path}: keel 依赖了应用层 {bad}"

def test_flowcoder_events_come_from_keel():
    # R1 验收：agent 包外不得再从 flowcoder.agent.events / flowcoder.agent import 事件名
    ...
```

规则随阶段递增：R1 加"事件必须来自 keel"、R2 加"五端口实现必须在 keel"、
R3 加"编码工具/prompt 不得进 keel"。

## 五、R1 —— 事件协议先行（预计 1 个会话）

### 5.1 范围与文件映射

| 动作 | 明细 |
|---|---|
| 新建 | `src/keel/__init__.py`、`src/keel/events/__init__.py`（15 事件 + PermissionResponse 原样搬迁） |
| 移动 | `agent/events.py` → 变 shim：`from keel.events import *  # noqa` + `__all__` 兼容导出 |
| 改写 | 17 个消费文件 + agent 包内引用的 import 路径 → `from keel.events import ...` |
| 新建 | `tests/architecture/test_dependencies.py`（护栏第一条规则） |
| 更新 | `pyproject.toml` build targets 增加 `src/keel`；ADR-R1 |

**刻意不做**：事件语义改动、字段增删、HookEvent 的去留讨论（登记到 R2）。

### 5.2 验收标准（全部客观可测）

1. 全量测试绿（1607+），`git diff` 无任何行为变更（仅 import/归属）；
2. `grep -rn "from flowcoder.agent.events" src/` 命中 0（shim 自身除外）；
3. 护栏测试 `test_flowcoder_events_come_from_keel` 通过；
4. `keel/` 内 `grep flowcoder` 命中 0。

### 5.3 风险

- **循环 import**：keel.events 是纯 dataclass 无依赖，风险极低；
- **`from flowcoder.agent import X` 混合导入**（18 处经包聚合导出）：`agent/__init__.py`
  改为从 keel.events re-export，聚合路径不变，消费方零改动即兼容——但护栏仍要求
  逐步收敛到直接 import keel。

## 六、R2 —— 五端口抽取（预计 2–3 个会话，每模块一步）

### 6.1 端口定义原则

端口 Protocol **从现有实现签名提炼**（不理想化重设计）——偏差逐条记入 ADR-R2。
五个端口的现有实现 → keel 映射：

| 步骤 | 内容 | compat 策略 |
|---|---|---|
| R2.1 | `keel/providers/`：client/base+core+factory+resilience+errors+error_mapping+context_window 原样升迁；`Provider` Protocol = LLMClient 现签名 | `flowcoder.client` shim re-export |
| R2.2 | `keel/sandbox/`：sandbox/ 全包升迁（含 pool/reaper/metrics），Sandbox 端口 = ContainerRuntime | `flowcoder.sandbox` shim |
| R2.3 | `keel/memory/`：Memory 端口（从 MemoryHub 公开方法提炼：load/observe/extract/recall）+ MemoryHub 实现 + providers | `flowcoder.memory` shim |
| R2.4 | `keel/policy/`：Policy 端口（`check(call) -> Decision`，从 PermissionChecker 的授权流程提炼）+ 四模式/规则引擎实现 | `flowcoder.permissions` shim |
| R2.5 | `keel/context/`：compaction/manager/tool_results 升迁；`keel/engine/`：budget + agent/core.py 循环骨架（策略经端口注入，编码相关 prompt/工具依赖反转出去） | `flowcoder.agent` shim |
| R2.6 | `keel/observe/`：TraceManager；`keel/engine/send_gate.py` | `flowcoder.agents.trace` shim |

**R2.5 是唯一有实质设计量的步骤**（core.py 的策略-机制分离），单独一个会话、
单独 ADR 记录端口签名偏差；若发现必须改行为才能解耦，停下汇报（PROMPTS 规则）。

### 6.2 每步验收

1. 全量测试绿；2. 该模块 shim 生效（旧 import 路径仍可用）；3. 护栏规则递增一条；
4. **该步骤完成后立即删上一阶段遗留 shim**（shim 存活不跨阶段）。

## 七、R3 —— 策略剥离与 app.py 清算（预计 2 个会话）

### 7.1 app.py 拆分（按 §2.3 映射表逐块搬）

每搬一块跑一次 TUI e2e（test_tui_critical_path）+ 全量测试。顺序：
ChatInput → 工具块渲染三件套 → SubAgentBlock → @ 引用 → SerializedSendGate（进 keel）
→ FlowCoderApp 主体（`profiles/coder/tui/app.py`，依赖注入 keel 引擎）。

### 7.2 profiles/coder 内容清单

bash/edit 工具注册、编码 system prompt（prompts.py）、skills 装配、
coder 权限策略默认配置、TUI 组件集、`/`命令集。
`flowcoder/__main__` 变成"Keel + coder profile"的三行组装。

### 7.3 验收

1. `grep -rn "bash\|edit\|coding" src/keel/` 命中 0（注释除外）；
2. app.py 不复存在（或 ≤100 行纯组装）；
3. overview.md / INDEX.md 更新到新结构（文档同步是 R3 验收项，非可选项）。

## 八、R4 —— 抽象验证裁判局（预计 1 个会话）

`profiles/minimal/`：CSV 问答 Agent，**只依赖 keel**——
Provider（已有配置即可用）+ 2 个新工具（read_csv / run_query，subprocess+白名单）+
Memory（记录问过的字段）+ Policy（全放行只读策略）。禁止改 keel 一行。

- 跑通：加载 CSV → 回答"某列总和/均值/Top-N" → 结构化输出；
- 每一处被迫改 keel 的冲动 = 抽象泄漏点 → 记入 `docs/architecture/keel-abstraction-report.md`
  （接口缺口 vs 不该有耦合，逐条定性）；
- 验收：minimal 跑通 + keel 零改动 + 报告完成 + CHANGELOG/README 更新 → **tag `v1.0.0-keel`**。

## 九、全局风险与回滚

| 风险 | 概率 | 对策 |
|---|---|---|
| 端口签名提炼歪了，R4 跑不通 | 中 | R4 是唯一裁判：回退到上一 tag，按泄漏报告修端口重走该步 |
| shim 双路径腐化 | 中 | 铁律：shim 不跨阶段存活；护栏测试检查旧路径命中数归零 |
| 循环 import（R2.5） | 中 | 端口定义与实现分文件；engine 只 import 端口文件 |
| 测试因 import 改写大面积失败 | 低 | R1 用聚合 re-export 过渡；护栏 green 后再收紧 |
| 与长时验收回填冲突 | 低 | R 阶段与真实环境回填互不依赖，可并行 |

回滚锚点：每阶段合并 main 后打 tag（建议 `v0.7.0-keel-events` / `v0.8.0-keel-ports` /
`v0.9.0-keel-profiles` / `v1.0.0-keel`），失败回退上一锚点。

## 十、执行节奏

延续"一次会话一个 Prompt"：R1 一个会话、R2 拆 2–3 个（每模块步一个）、R3 拆 2 个、
R4 一个。每会话结束按惯例汇报改动清单 + 验收对照 + 是否打 tag。
