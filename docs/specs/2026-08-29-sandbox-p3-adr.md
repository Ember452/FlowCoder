# ADR：可靠性加固（P3）

- 状态：Accepted（e2e 三条已入库；真实 Docker 沙箱 e2e 待环境回填）
- 日期：2026-08-29
- 关联：PROMPTS.md P3、docs/architecture/llm-client.md、docs/architecture/agent-loop.md

## 背景

LLM 调用面对 429/5xx/网络抖动/挂死四类瞬时故障，Agent 循环对长任务无资源
上限约束。P3 新建韧性层（重试/退避/限流）与四维预算闸，补三条 e2e。

## 决策与理由

### D1：resilience 并入 client 包（而不是顶层 resilience.py）

PROMPTS.md 允许二选一。选择 `client/resilience.py`：重试分类依赖
`client.errors` 的 LLMError 体系（RateLimitError 带 retry_after、NetworkError、
新增 ServerError/LLMTimeoutError），而 client 是最底层——顶层 resilience.py
会形成对 client 的依赖之外再包一层的反向结构。放在 client 包内保持
"client = LLM 调用的全部关切"的内聚。

### D2：流式重试的边界——只重试"零交付"失败

流式响应中途失败无法重放（事件已交付消费方）。规则：错误发生在任何事件
yield 之前 → 可整体重试（重放成本为零，消费方无感知）；首事件之后失败 →
原样抛出。`ResilientClient` 用 yielded 标记在外层循环判定（首版把标记放
在 _stream_attempt 内部，测试 `test_no_retry_after_first_event_yielded`
抓出作用域错误后修正）。

### D3：错误分类与退避

- 可重试：RateLimitError（尊重 retry-after 头，与本地指数计算取大者）、
  NetworkError、ServerError（5xx，error_mapping 新增映射）、LLMTimeoutError
  （新增：单请求超时）。
- 不可重试：AuthenticationError 与 4xx——立即失败，不烧重试预算。
- 退避：指数 `min(max_s, base·2^attempt) + U(0, jitter)`，总封顶
  `max_s + jitter`；retry_after 优先取大（provider 限流窗口是权威）。
- 限流：进程内异步令牌桶（RPM），惰性按时间补充，容量封顶；acquire 全
  asyncio 原语不阻塞事件循环。
- 默认值：max_retries=2（默认开启——瞬时故障自愈是出厂能力而非可选项），
  经 `ProviderConfig.max_retries / rate_limit_rpm / request_timeout_s` 配置，
  `create_client` 工厂统一包裹，Agent/TUI/eval 全部消费方零改动获得韧性。
- 超时语义：`request_timeout_s` 是"无事件产出"的兜底（事件持续产出则续期），
  覆盖"连接成功但永远不出首事件"的挂死场景。

### D4：预算闸——"总结并收敛"而非硬杀

- 四维：token（输入+输出累计）/ 轮次 / 挂钟时间 / 成本（token × 可配置单价）。
  判定逻辑独立为 `agent/budget.py`（纯函数可单测），core.py 只做最小接入
  （构造参数 + 循环顶部一次判定 + 收敛轮撤工具，diff 约 40 行，PROMPTS 允许
  并单独说明）。
- 两阶段收敛：首次超限 → 注入收敛请求（user 消息）+ 撤下工具 schema
  （模型无工具可调，自然走"无 tool_call 即收敛"的既有路径）→ 若模型收敛，
  LoopComplete 正常产出（与正常结束不可区分）；若收敛轮仍超限或仍幻觉
  工具调用 → ErrorEvent + LoopComplete 强制结束。对比原有的
  max_iterations 硬截断，预算耗尽的任务能带着一份进展总结收场。
- 默认不设预算（None）：行为与从前完全一致，全部既有测试零改动通过。
- 配置入口为编程式（`Agent(budget=Budget(...))`），未加 config.yaml 段：
  预算阈值与任务强相关（daemon 每任务不同），配置文件表达不了这种动态性。

### D5：e2e 三条的取舍

- TUI 关键路径：Textual `run_test()` 驱动真实 FlowCoderApp 装配（单 provider
  自动选择 → Agent 构建 → _send_message 完整回路），仅 create_client 打桩
  为 fake LLM。断言到对话状态层（conversation 消息），不锁 UI 细节。
- daemon 断线重连：TestClient + websocket。断开期间事件持续落账
  （records.emit），重连后按序补投 + ReplayDone + 会话可继续交互；
  另覆盖服务重启后会话从磁盘恢复。
- 沙箱内任务完成：真实容器 e2e（docker marker，无 daemon 自动跳过）——
  传文件 → 执行 → 结果回传 → 归还后环境全新 → close 后无残留。

## 待办与验收回填

- [ ] 真实容器沙箱 e2e（tests/e2e/test_sandbox_task_complete.py）待 Docker
      环境实测通过
- [ ] 混沌演练（429 风暴/断网/杀容器）在 P4 以 scripts/chaos.py 系统化执行
