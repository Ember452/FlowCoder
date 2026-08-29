# ADR：HumanEval+ 评测流水线（P2a）

- 状态：Accepted（真实 LLM 跑 50 题的第一份报告待本机执行，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P2a、TRANSFORMATION_PLAN.md Phase 2、docs/architecture/eval.md

## 背景

新建 `src/flowcoder/eval/`，对 HumanEval+（EvalPlus 格式，164 题，默认取前 50）
做 pass@1 评测：逐题调用 Agent 循环生成解法，在沙箱中执行测试。约束：评测是
Agent 的消费者，不改 agent/core.py；单测不依赖真实 LLM（fake provider）与
真实 Docker（既定决策）。

## 决策与理由

### D1：评测是 Agent 的消费者——双 Protocol 注入，零改核心循环

- 生成侧：`SolutionSolver` Protocol。`LiveAgentSolver` 驱动 `agent.run()` 事件流
  （消费 `StreamText` 累积输出、`UsageEvent` 累计 token、`ErrorEvent` 记错、
  `LoopComplete` 记轮次），每题一个全新 `ConversationManager`。测试注入
  `FakeSolver`。Agent 循环的分区分批、权限门、压缩等机制原样复用。
- 执行侧：`SandboxExecutor` Protocol。`DockerSandboxExecutor` 包装 sandbox 模块
  的容器池（P1b 复用），测试注入本地 `python -c` 执行的 fake 或记录型 fake。
- 为什么协议化：dataset/runner 不绑死 LLM 实现与沙箱实现，fake/真实两态切换
  只是换注入对象；这也是"评测跑在 Agent 旁边而不是改 Agent"的结构保证。

### D2：评测执行走沙箱，权限模式固定 bypassPermissions

- 生成的代码是不可信输入，与 P1a 的安全论证一致：资源限额、断网、只读根 +
  白名单挂载、归还即销毁。
- 评测是无人的批量消费场景，没有人应答 `PermissionRequest`；评测提示不要求
  工具调用（直接要代码块），bypassPermissions 只是兜底——若模型仍调用工具，
  允许其执行而不会挂死批跑。权限语义的正式论证见 P1c ADR D2。

### D3：轻量 check() harness + special-oracle 题跳过（本 ADR 最重要的发现）

- evalplus 官方 runner **不执行** test 片段里的 `check(candidate)`：它用
  预计算的 `plus_input`/`expected_output` 逐输入调用 `fn(*inp)` 与期望值比较，
  特殊题（如 HumanEval/32）走 special oracle（`evalplus/eval/_special_oracle.py`）。
- 因此数据里的 test 片段并非全部可独立执行。标准形状
  （`def check(candidate): assertion(candidate(*inp), exp, 0)`）拼上
  `check(entry_point)` 即可正确判定；但 special-oracle 题（识别特征：test 含
  `*candidate(` 的返回值解包变换，如 /32 的
  `assert _poly(*candidate(*inp), inp)`）对正确解法也会误报失败。
- 决策：`is_harness_compatible()` 过滤，special-oracle 题跳过并在指标中单列
  `skipped`（不计入 pass@1 分母）。前 50 题实测：1 题跳过（HumanEval/32）。
  这使我们的 pass@1 与 evalplus 官方数字存在已知的、已量化的口径差
  （我们只对可兼容题负责）。若后续要求全量精确对齐，再评估移植 evalplus
  的预计算输入方案。

### D4：numpy 依赖用预构建镜像解决，运行期保持断网

前 50 题实测 49/50 的测试片段 import numpy；slim 镜像无 numpy，而沙箱默认
断网（P1a D2）无法在容器内安装。逐租借安装既慢又需要放开网络（破坏安全默认）。
决策：`scripts/Dockerfile.eval` 预构建 `python:3.11-slim + numpy` 镜像（走项目
统一的阿里 PyPI 源），`python -m flowcoder.eval --image flowcoder-eval:py311`
一次构建反复使用，容器运行期仍然断网。

### D5：dataset / runner / metrics / report 四段解耦

- 换数据集（MBPP+、SWE-bench Lite）只动 datasets/，runner 不变；
- 换判定通道（fake/本地 python/Docker）只动 executor 注入；
- metrics 只吃 `ProblemResult` 列表（纯函数），report 只做序列化——
  P2b 的自愈循环与 k-sample 扩展在 runner 内进行，报告格式稳定。

### D6：温度固定的限制（如实记录）

"温度固定、每题 1 trial" 中，trial 数由 runner 控制（P2a 每题 1 trial）；
但 LLMClient 三协议均未暴露 temperature 接线（Anthropic 还存在 thinking 模式
强制 temperature=1 的约束），本阶段不改 client/providers 接口。报告 meta 如实
标注 `temperature: provider-default`。显式温度接线推迟到 P2b（k-sample 可复现
性要求出现时一并实现），这是本阶段唯一的已知口径缺口。

## 交付物

- `src/flowcoder/eval/`：datasets/（加载器 + 内置玩具题）、runner.py、
  metrics.py、report.py、`__main__.py`（CLI：`python -m flowcoder.eval`）
- `scripts/download_humaneval_plus.py`：下载 + sha256 校验
  （`908377f1daf28dcb36846db73a5662b2e05a9907407c2696c89ad9d3b0b04492`，
  与 HF LFS oid 一致，164 题，2026-08-29 下载核对）
- `scripts/Dockerfile.eval`：numpy 预装镜像
- `eval-data/` 与 `eval-results/` 均不入 git

## 待办与验收回填

- [ ] 本机执行真实 LLM 50 题产出第一份报告（需 API key + Docker 就绪；
      无 Docker 时用 `LocalPythonExecutor` 等价物跑通流程的降级方案未内建，
      如实等待环境）
- [ ] P2b：显式 temperature 接线
- [ ] 口径说明：本流水线 pass@1 分母 = 兼容轻量 harness 的题数（前 50 题为 49）
