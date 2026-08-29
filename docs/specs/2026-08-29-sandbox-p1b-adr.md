# ADR：沙箱容器池化与泄漏回收（P1b）

- 状态：Accepted（真实容器验收项延后，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P1b、docs/specs/2026-08-29-sandbox-p1a-adr.md、docs/architecture/sandbox.md

## 背景

P1a 的单容器模型每次执行都要冷启动容器（拉起 sleep infinity + create/start 往返），
高频执行下开销显著；且调用方异常退出时容器无人回收，会累积泄漏。P1b 新增
`pool.py`（SandboxPool）、`reaper.py`（LeaseReaper）、`metrics.py`（SandboxMetrics）。

## 决策与理由

### D1：预热租借 vs 每次冷启动

- 预热：池 `start()` 时并发启动 N 个容器（默认 10），租借是 deque 左弹（O(1)）。
  执行延迟从"冷启动数百 ms~秒级"降到"池内 exec 一次往返"。
- 归还即销毁、后台补建同规格容器：牺牲补建的异步成本，换取**每次执行环境全新**
  ——不可信代码可能污染容器（写文件、装东西、改状态），复用脏容器的安全隐患
  大于补建开销。因此"复用"只发生在同一次租借内的多次 execute 之间（指标
  reuse_count 语义），跨租借不复用。
- 替代方案"归还时重置状态"需要 diff 容器状态，不可靠且复杂，放弃。

### D2：队列背压 vs 快速失败——都要，分层设防

- 池耗尽时请求在 `asyncio.Condition` 上排队等待（背压）：调用方无需处理重试，
  短时突发流量被平滑。
- 等待者数量超过 `max_queue`（可配置）时抛 `PoolExhaustedError`（快速失败）：
  防止上游无限堆积导致内存膨胀与超时雪崩。`max_queue=None` 表示不限（默认），
  由调用方按负载特征决定；daemon 场景建议显式设置。
- 背压在 Condition 上实现而非 asyncio.Queue：Queue 无法直接拿到"当前等待者数"，
  Condition 方案用 `self._waiting` 计数显式控制上限。

### D3：健康检查内建于租借路径

空闲容器可能被外部 `docker kill`。租借前对候选容器做 `is_alive` 体检（runtime
协议新增操作，Docker 侧为 get+reload 查 status）：死容器销毁、后台补建、自动
改取下一个。不引入后台巡检线程——把检查放在唯一会暴露问题的路径（租借）上，
成本 O(1) 且逻辑闭环。执行中途被 kill 的容器由双层超时/异常路径收场（P1a），
归还时销毁重建自然清掉。

### D4：reaper 对账与启动清理

- 租借台账 `LeaseRecord(container_id, task_id, leased_at)`：reaper 周期对账，
  归属任务已结束但容器未归还的判为孤儿，强制销毁并补建。"任务是否存活"由调用方
  注入 `is_task_running` 回调（daemon 任务注册表知道答案，sandbox 不感知上层
  任务模型——依赖方向约束）。
- 无 task_id 的租借不判孤儿（信息不足，宁漏勿错杀）。
- 启动清理：池 `start()` 先按 `flowcoder.sandbox` label 全量清扫上一次运行
  遗留的容器再预热。用 label 而不是进程内状态识别归属，因为"上次运行"的状态
  本来就不在本进程手里。
- reaper 销毁失败（daemon 抖动）只记日志不抛：下一轮对账会再试，台账已移除
  避免重复判孤儿。

### D5：runtime 协议追加三个操作（is_alive / list_by_label / stats）

P1a 的六操作协议不够池化使用。追加：
- `is_alive`：租借体检（D3）；
- `list_by_label`：启动清理孤儿容器；
- `stats`：一次性采样 memory/cpu，供资源峰值指标（best-effort，失败跳过）。
协议仍是同步阻塞风格，调用侧全部 `asyncio.to_thread`，与 P1a 一致。

### D6：指标与 TraceManager 的接入方式

- 聚合指标（租借等待、复用次数、执行耗时、资源峰值）在 `SandboxMetrics` 只留
  总量/峰值，不留逐次明细，避免长期运行内存无界。
- TraceManager 是 Agent 调用树追踪器（`agents/trace.py`）。sandbox 若直接 import
  它，违反"sandbox 不反向依赖上层"的依赖方向。因此定义鸭子类型 `TraceSink`
  Protocol（create/update/complete 三方法），`TraceManager` 结构上天然满足：
  每次租借在 trace 树上建 `sandbox_pool` 节点，每次 execute 更新 tool_call_count，
  归还时以 completed/failed/timeout/error 收口。池零依赖地接入现有可观测面。

## 后果

- 池默认 size=10，daemon 场景需评估宿主内存（每容器 256MB 限额，预热即占
  ~2.5GB 上限）。size 与 max_queue 均可配置。
- 真实容器验收项延后：`tests/integration/sandbox/test_pool_real.py` 承载
  并发执行与补建、kill 空闲容器后池自愈、启动清理遗留容器三场景，无 Docker
  显示 skip；待环境就绪实测后回填本 ADR 与简历数据（20 并发压测无泄漏、
  冷启动消除对比数据）。

## 真实容器验收回填（待 Docker 环境就绪）

- [ ] test_concurrent_execution_and_rebuild 实测通过（9 请求 / 池 3）
- [ ] test_killed_idle_container_replenished 实测通过
- [ ] test_startup_clears_abandoned_containers 实测通过
- [ ] 20 并发压池：docker ps 对账无泄漏，记录 P50/P99 与租借等待分布
- [ ] 冷启动消除数据：P1a 每次新建容器耗时 vs 池租借耗时对比
