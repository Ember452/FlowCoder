# 评测（eval/）

`src/flowcoder/eval/` 是 HumanEval+ 评测流水线（P2a）。评测是 Agent 的**消费者**：
驱动 `agent.run()` 事件流取解法、复用 sandbox 模块执行测试，不改 agent/core.py。

## 模块结构

| 模块 | 职责 |
|---|---|
| `datasets/` | `Problem` 模型 + HumanEval+（EvalPlus 格式）JSONL 加载器（支持 .gz、limit）+ sha256 校验 + 内置玩具题（单测用） |
| `runner.py` | `EvalRunner`：生成（`SolutionSolver` Protocol）→ 代码提取 → harness 拼装 → 沙箱执行（`SandboxExecutor` Protocol）；`asyncio.Semaphore` 限并发；`LiveAgentSolver`（真实 Agent 循环）与 `DockerSandboxExecutor`（容器池）为真实实现 |
| `metrics.py` | pass@1（分母=已评测题）、skipped/超时/生成错误计数、平均 token 与耗时 |
| `report.py` | Markdown + JSON 报告写入 `eval-results/`（不入 git） |
| `__main__.py` | CLI：`python -m flowcoder.eval --dataset ... --limit 50 --image ...` |

## 一次评测的流程

```
load_problems(eval-data/humaneval_plus.jsonl, limit=50)
  → EvalRunner.run(problems)                     # Semaphore(concurrency) + gather
     ├─ special-oracle 题 → skipped 结果          # is_harness_compatible 过滤
     └─ 每题：
        ① LiveAgentSolver.solve(prompt)          # agent.run() 事件流：StreamText/UsageEvent/ErrorEvent
        ② extract_code(text)                     # 最后一个 ```python 围栏块，无围栏取全文
        ③ build_test_harness(problem, code)      # 解法 + test 片段 + check(entry_point)
        ④ executor.run_test(files, timeout_s)    # 容器池执行 python run_test.py
  → compute_metrics(results)
  → write_report(...)                            # eval-results/report-<ts>.md / .json
```

## 关键设计决策（详见 docs/specs/ 三篇 ADR）

1. **双 Protocol 注入**：`SolutionSolver`（会话式：start → ask） / `SandboxExecutor` 使 fake 与真实实现可互换，评测不改核心循环。
2. **special-oracle 跳过**：evalplus 官方 runner 用预计算输入 + special oracle，不跑 test 片段的 check()；含 `*candidate(` 变换的题对轻量 harness 会误判，跳过并单列指标。
3. **numpy 预装镜像**：测试片段大量依赖 numpy 且沙箱断网，`scripts/Dockerfile.eval` 构建一次镜像解决，运行期保持断网安全默认。
4. **权限模式 bypassPermissions**：无人批跑场景的兜底（评测提示不要求工具调用）。
5. **自愈闭环**（P2b）：失败输出喂回同一会话，最多 heal_rounds（默认 3）轮修复；超时/生成错误不重试；逐轮记录 token 与结果。
6. **k-sample 首胜**（P2b）：同题并行 k 个独立 trial（独立 Agent 会话），首个通过即胜出、其余 cancel（CancelledError 放行）；取消 trial 未完成轮的 token 不可观测不计入。
7. **失败四分类**（P2b）：超时/轮次耗尽→超预算；compile 失败→编译错；AssertionError→逻辑错；其他运行时异常→测试理解错（启发式，局限见 ADR）。
8. **温度固定**（P2b）：`--temperature` 默认 0.0，经 `ProviderConfig.temperature` 透传三协议；thinking 模式互斥不传。

## 使用

```bash
python scripts/download_humaneval_plus.py                  # 下载 + sha256 校验到 eval-data/
docker build -t flowcoder-eval:py311 -f scripts/Dockerfile.eval .
python -m flowcoder.eval --image flowcoder-eval:py311      # 默认 50 题、k=3、自愈 3 轮
python -m flowcoder.eval --compare --image flowcoder-eval:py311   # 对比矩阵 → comparison-<ts>.md
```

## 当前边界

- 与 evalplus 官方数字存在已知口径差：pass@1 分母 = 兼容轻量 harness 的题数。
- 被取消 trial 的未完成轮 token 不可观测，不计入成本统计（口径见 P2b ADR）。
