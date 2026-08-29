# ADR：自愈闭环与失败分类（P2b）

- 状态：Accepted（50 题真实对比报告待环境就绪执行，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P2b、docs/specs/2026-08-29-sandbox-p2a-adr.md

## 背景

在 P2a 流水线上增加：自愈闭环（测试失败输出喂回 Agent 修复重跑）、失败四分类、
k-sample 首胜、对比报告。验收：无自愈 vs 有自愈、k=1 vs k=3 的对比数字可复现。

## 决策与理由

### D1：温度固定接线（补齐 P2a 遗留的口径缺口）

`ProviderConfig.temperature`（None=provider 默认）透传到三协议请求构造。
Anthropic API 约束 thinking 与 temperature 互斥——thinking 开启时不传。
CLI `--temperature` 默认 0.0：评测验收要求"数字可复现"，贪心采样不可复现，
固定 0 是评测场景的正确默认；交互场景不受影响（不设即 None）。

### D2：Solver 协议改为会话式（start → ask）

自愈修复轮需要把失败输出喂回**同一个对话**（Agent 记得自己此前的解法，
修复提示才完整），单发式 `solve(prompt)` 表达不了多轮状态。会话式接口：

```
solver.start() → session.ask(生成提示) → session.ask(修复提示)...
```

同时解决一个隐藏问题：k-sample 的并行 trial 不能共享 Agent 实例
（Agent 内部有 recovery_state、token 计数等可变状态）。
`LiveAgentSolver` 接受 agent_factory，每个会话创建独立 Agent，
client/registry 等昂贵对象复用。

### D3：自愈闭环——修复轮语义与边界

- 轮次：1 次生成 + 最多 heal_rounds（默认 3）次修复。heal_rounds=0 完全
  等价 P2a 单轮行为。
- 修复提示包含：失败 stderr/stdout（截断 2000 字符）+ 原题。反馈截断防止单题
  失败输出撑爆上下文。
- **超时不重试**：执行超时意味着没有可读的失败信息（或进程挂死），
  喂回"timed out"对模型无信息量，直接终止该 trial。
- **生成错误不重试**：LLM 调用错误（429 等）由重试策略层（P3）负责，
  修复轮只为"有测试失败信号"的情境服务。
- 逐轮记录 RoundRecord（token、退出码、耗时、错误），成本可审计。

### D4：失败四分类——输出特征启发式（附局限）

`failure_tax.classify_failure` 只对"已执行且未通过"的题分类：

1. 超预算：执行超时，或修复轮次用尽（仅当 heal_rounds>0 且确实用尽）；
2. 编译错：`compile()` 失败，或 stderr 出现 SyntaxError 族；
3. 逻辑错：AssertionError——跑得通但算错值，程序逻辑错误的直接信号；
4. 测试理解错：其他运行时异常（TypeError/KeyError/AttributeError 等）——
   通常源于误解签名/类型/返回值约定。

局限（如实记录）：③④ 的边界是启发式代理——纯输出特征无法完全区分
"逻辑算错"与"题意理解偏"（off-by-one 也可能以 TypeError 暴露）。
分类用于失败分布观察，不作为自动重试或结果修正的依据。
优先级：超时 > 编译 > 轮次耗尽 > 异常类型 > 兜底逻辑错
（编译错优先于轮次耗尽：代码本身坏了比"没预算了"更有诊断价值；
heal_rounds=0 时不存在"预算耗尽"概念）。

### D5：k-sample 首胜——取消语义

- 同题并行 k 个独立 trial（各自会话 + 各自沙箱执行），`asyncio.wait(FIRST_COMPLETED)`
  循环收割；某个 trial 通过即胜出，`task.cancel()` 其余并
  `gather(return_exceptions=True)` 收尾——CancelledError 放行（AGENTS.md 第五节），
  gather 的 return_exceptions 只做清理汇总，不阻断向调用方的传播路径。
- 无胜者（全部失败）时全部自然完成，不 cancel。
- 成本口径：**被取消 trial 的未完成轮 token 不可观测，不计入**；已完成的轮次
  （含被取消 trial 的）如实计入。报告标注该口径。
- 曲线数据：对比报告给出 k=1 vs k=3 的 pass@1 与平均 token 成本，
  即"k 值 vs 单位成本"曲线的两个采样点（更多 k 值加配置行即可）。

### D6：对比报告（验收载体）

`write_comparison_report` 产出对比总表（pass@1、自愈回收率、失败分布、
平均 token/耗时、平均取消数）+ 逐题通过矩阵（50 题 × 各配置，skipped 显示
⏭️）。CLI `--compare` 跑标准矩阵：k=1,heal=0（无自愈）/ k=1,heal=3（有自愈）/
k=3,heal=0（k-sample 首胜）。同一数据集、同一温度（0.0）下数字可复现。

## 交付物

- `runner.py`：会话式 Solver、自愈循环、k-sample 首胜、RoundRecord/TrialRecord
- `failure_tax.py`：四分类启发式
- `metrics.py`：自愈回收率、失败分布、trial 平均数
- `report.py`：`write_comparison_report`
- `__main__.py`：`--heal-rounds / --k / --temperature / --compare`
- `config` + client 三协议：temperature 接线

## 待办与验收回填

- [ ] 本机执行 `python -m flowcoder.eval --compare`（50 题，需 API key + Docker）
      产出第一份真实对比报告，数字回填本 ADR 与简历素材
- [ ] "k 值 vs 单位成本 pass@1" 曲线：k=1/k=3 两点已内建，扩展 k 值按需加矩阵行
