# ADR：仓库看门狗（P5b）

- 状态：Accepted（"连续运行 1 天"的真实时钟验收待用户执行，测试内已用
  加速时间线覆盖等价断言，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P5b、docs/architecture/watchdog.md、
  docs/specs/2026-08-29-scheduler-p5a-adr.md

## 背景

新建 `src/flowcoder/watchdog/`：监听仓库信号，Agent 判断"是否值得主动
提示"，经防骚扰门控后送达。要求：可插拔信号源、prompt 内置标准 + 结构化
输出判定、冷却/去重/每日上限 + 多时间尺度衰减、全状态持久化（重启不重发）。

## 决策与理由

### D1：ProactiveGate 思想借鉴、代码重写

flow-agent 的防骚扰三件套（冷却间隔 / delivery_key 去重 / 每日上限）思想
照搬，实现按本项目风格重写为纯数据结构 + 纯判定函数（`GateState` +
`ProactiveGate.decide/record_delivery`），持久化外置（与 P5a store 同构：
原子写、损坏兜底）。**判定顺序有讲究**：

```
去重（O(1) 查表，在花 LLM token 之前拦下重复事件）
→ Agent 判定（"值不值得"，消耗 token 的环节放最靠后的必经之路）
→ 门控（冷却/每日上限/衰减——判定管"值不值得"，门控管"礼不礼貌"）
→ 送达 → record_delivery
```

### D2：delivery_key 刻画内容而非时间

同一份脏工作区反复 poll 产出**同一个 key**（内容 hash），被去重；状态
变化产出新 key 才再次进入判定。测试结果恶化的 key 绑定 (baseline, 当前)
对——同样的降幅从不同 baseline 算不同事件。信号源实现方保证同源不重复
吐同 key，避免每轮重复消耗判定调用。

### D3：Agent 判定——prompt 内置标准 + 保守降级

- 判定 prompt 内置四条标准（全部满足才提示）：用户需要知道 / 不及时说有
  损失 / 无法被动获知 / 一次说清；并给出反例（纯状态同步类）压低提示欲。
- 结构化输出 `{"worth_prompting": bool, "reason": str}`；**解析失败、
  字段非布尔、LLM 调用失败一律按"不值得"处理**——看门狗的失败模式应当
  是漏报而非骚扰（保守降级是主动式 Agent 的安全默认）。

### D4：多时间尺度能量衰减（第四层门控）

E(t) = Σ_历史 Σ_尺度 α_i·exp(-Δt/τ_i)，双尺度 (α=1.0, τ=1h) + (α=0.5,
τ=24h)，容量 3.0。相比每日上限的"硬截断"，能量模型让频率限制平滑：
短时间内连续提醒快速耗尽容量，间隔拉长后自然恢复。公式自实现，历史
送达时刻滚动上限 200 条。

### D5：持久化与"重启不重发"

`GateStateStore`（与 P5a store 同构：原子写、损坏从空开始）持久化
delivered_keys（去重全集，滚动 500）/ delivery_times / daily_counts /
last_delivery_at。重启后同一 key 再次出现会被去重拦截——"已发过的提醒
不重发"的依据是**内容**（key 持久化）而非进程内状态。

### D6：与 P5a 的关系

watchdog 不依赖 scheduler——两者共享同一套可测轮询模型（poll_once +
合成时钟）与 store 持久化模式，但编排职责不同：scheduler 是"到点必触发"
的确定性值守，watchdog 是"信号驱动、宁缺勿滥"的主动观察。若要每 N 小时
巡检一次仓库，可将 watchdog.poll_once 作为 scheduler 的任务提交执行
（组合而非耦合）。

## 验收对照

- ✅ 去重命中 / 冷却拦截 / 每日上限 / 重启后状态恢复 / Agent 判定
  "不值得说"不提醒：单测 21 例
- ✅ 连续运行 1 天：24h 加速时间线等价断言——12 批事件（11 批含重复的
  同 key 信号）触发/拦截完整记账、送达 key 全唯一（0 重复推送）、
  每日上限生效
- ⏳ 真实时钟 1 天验收：留作用户侧执行（信号源接真实 git 仓库 +
  LLMJudge 接真实 provider，账目在 WatchdogReport）
