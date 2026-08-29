# 混沌演练报告（P4）

- 日期：2026-08-29
- 执行环境：Windows 10 / Python 3.13，无 Docker（容器场景为 fake runtime 演练，
  真实容器版已内建 `--docker` 开关，待环境就绪补测）
- 注入工具：`scripts/chaos.py`（自包含，不依赖 pytest；结果 JSON 落
  `chaos-results/`，不入 git）
- 本次运行产物：`chaos-results/chaos-20260829-214730.json`——**5/5 场景通过**

## 复现命令

```bash
python scripts/chaos.py --all                # 全部场景（断网默认 30s）
python scripts/chaos.py --all --docker       # 附加真实容器 kill 场景（无 Docker 自动跳过）
python scripts/chaos.py --only llm-outage --outage-s 30
python scripts/chaos.py --only pool-exhaustion,rate-limit-storm
```

## 场景结果

### 1. pool-exhaustion（容器池耗尽）——✅

- **注入**：池 size=2、max_queue=3，并发 10 个执行请求（fake runtime，exec 即返）；
  第二阶段 max_queue=1 复核：3 个并发等待者。
- **预期**：至多 5 个立即执行（2 在跑 + 3 排队），其余以 `PoolExhaustedError`
  快速失败；无挂死、无其他错误、无泄漏。
- **实测**：5 个排队后成功、5 个快速失败（全部为 PoolExhaustedError，其他错误
  0 个，总耗时 1ms，池水位恢复 idle=2）；max_queue=1 复核 2/3 快速失败。
- **结论**：排队背压与快速失败分层（P1b ADR D2）行为正确。**过程中发现并修正
  演练脚本自身的判定缺陷**：首版断言"10 个全部成功"，与 max_queue=3 的设计
  语义矛盾——正确的验收是"成功 + 快速失败 = 全部请求，且失败原因全部为
  PoolExhaustedError"。

### 2. container-kill（容器被外部 kill）——✅（fake 层）

- **注入**：池预热 size=3 后模拟外部 `kill -9` 全部容器，随后发起 3 个执行。
- **预期**：租借前健康体检淘汰死容器（销毁 + 后台补建），任务全部成功。
- **实测**：3 个执行全部成功（exit 0）；3 个死容器全部在租借体检时被淘汰，
  补建 6 个（3 个即时补建 + 3 个归还补建），存活 3/3。
- **结论**：租借体检自愈路径（P1b ADR D3）闭环。真实容器的 `docker remove -f`
  版本已内建（`--docker`），待 Docker 环境执行后回填本节。

### 3. llm-outage（LLM 断网 30s）——✅

- **注入**：`FaultInjectionClient` 对 30s 窗口内的所有请求抛 `NetworkError`，
  之后恢复；`ResilientClient`（max_retries=12，标准退避 base 0.5s / 封顶 8s）包裹，
  驱动真实 Agent 循环。
- **预期**：断网期间持续退避重试，窗口结束后自动恢复并收敛，完成率 100%。
- **实测**：总耗时 32.5s（断网 30s + 恢复后 2 轮对话 2.5s），9 次 stream 调用
  （8 次失败重试 + 恢复），任务正常收敛（LoopComplete）。
- **结论**：韧性层把 30s 断网完全吸收，**恢复成功率 100%**。**过程中发现并修复
  注入器缺陷**：时窗模式被计数模式守卫短路导致断网从未生效；同时确认缩时退避
  （max 0.2s）覆盖不了 30s 窗口，标准退避参数（8 次重试累计 ~39.5s）才是断网
  场景的正确配置——该配置结论已反映在场景代码注释中。

### 4. rate-limit-storm（429 风暴）——✅

- **注入**：连续 8 次 `RateLimitError(retry_after=0.05)`，第 9 次起恢复；
  `ResilientClient`（max_retries=10）包裹，真实 Agent 循环。
- **预期**：8 次 429 全部被吸收，恢复后正常收敛，完成率 100%。
- **实测**：8 次注入全部吸收（每次重试尊重 retry-after，退避 0.1~0.4s），
  恢复后 2 轮对话收敛，总耗时 2.88s。
- **结论**：429 风暴对 Agent 循环与上层完全无感知——限流治理收敛在
  client 韧性层。

### 5. budget-exceeded（任务超预算）——✅

- **注入**：`Agent(budget=Budget(max_total_tokens=300))`，脚本化 client 每轮
  产出工具调用（每轮 110 tokens）。
- **预期**：超限后注入收敛请求并撤下工具 schema，模型总结后以 `LoopComplete`
  正常收场（非硬杀、无 ErrorEvent）。
- **实测**：第 4 轮开头触发（累计 330 > 300），收敛请求注入对话、工具 schema
  撤下，模型纯文本总结后正常收敛；ErrorEvent 未出现。
- **结论**：两阶段收敛（P3 ADR D4）语义正确——超预算任务带着进展总结收场，
  而非硬杀丢弃全部上下文。"无赖模型收敛轮仍调工具"的强制收场路径由
  `tests/unit/agent/test_budget.py` 单测覆盖。

## 压测数据表

| 场景 | 并发/规模 | 成功 | 快速失败/吸收 | P50 延迟 | 总耗时 | 恢复成功率 |
|---|---|---|---|---|---|---|
| pool-exhaustion | 10 并发 / 池 2 / 队列 3 | 5（排队后） | 5（PoolExhausted） | ~0ms* | 1ms | 100%（成功+明确失败=10/10） |
| container-kill | 3 执行 / 池 3 全灭 | 3 | 0 | ~0ms* | 100ms | 100%（3/3 自愈） |
| llm-outage | 30s 断网窗口 | 收敛 | 8 次失败全部重试吸收 | — | 32.5s | 100% |
| rate-limit-storm | 8 连击 429 | 收敛 | 8 次全部吸收 | — | 2.88s | 100% |
| budget-exceeded | 300 token 预算 | 收敛 | — | — | <100ms | 100%（无硬杀） |

\* fake runtime 的 exec 即返，延迟数字反映编排开销而非容器执行；
真实容器的 P50/P99 压测（20 容器池）待 Docker 环境补测。

## 待环境就绪的回填项

- [ ] `python scripts/chaos.py --all --docker`：真实容器 kill -9 场景实测
- [ ] 真实容器池 20 并发压测：docker ps 对账无泄漏、P50/P99、冷启动消除数据
      （P1b ADR 回填清单）
- [ ] LLM 断网演练叠加真实 provider（当前为故障注入 client 驱动真实 Agent 循环
      + 真实韧性层，故障语义与真实 429/断网一致）
