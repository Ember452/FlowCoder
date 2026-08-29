# Agent 核心循环

ReAct 推理-行动主循环：LLM 调用 → 工具授权 → 执行 → 结果回填 → 压缩。

## 预算闸（P3）

四维预算（token / 轮次 / 挂钟时间 / 成本）在循环外围强制执行：

- 判定逻辑独立于 `agent/budget.py`（`Budget` + `BudgetState`，纯函数）；
  core.py 的 `run()` 循环只在每轮开头做一次判定（最小接入）。
- 超限**不硬杀**：注入收敛请求（user 消息）并撤下工具 schema，模型自然走
  "无 tool_call 即收敛"路径，产出进展总结后以 `LoopComplete` 正常收场；
  收敛轮仍超限或仍调用工具 → `ErrorEvent` + `LoopComplete` 强制结束。
- 默认不设预算（`Agent(budget=None)`），行为与无预算完全一致；
  预算为编程式配置（`Agent(budget=Budget(...))`），阈值与任务强相关，
  不进静态配置文件。
