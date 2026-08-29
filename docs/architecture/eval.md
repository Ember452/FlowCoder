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

## 关键设计决策（详见 docs/specs/2026-08-29-sandbox-p2a-adr.md）

1. **双 Protocol 注入**：`SolutionSolver` / `SandboxExecutor` 使 fake 与真实实现可互换，评测不改核心循环。
2. **special-oracle 跳过**：evalplus 官方 runner 用预计算输入 + special oracle，不跑 test 片段的 check()；含 `*candidate(` 变换的题对轻量 harness 会误判，跳过并单列指标。
3. **numpy 预装镜像**：测试片段大量依赖 numpy 且沙箱断网，`scripts/Dockerfile.eval` 构建一次镜像解决，运行期保持断网安全默认。
4. **权限模式 bypassPermissions**：无人批跑场景的兜底（评测提示不要求工具调用）。

## 使用

```bash
python scripts/download_humaneval_plus.py                  # 下载 + sha256 校验到 eval-data/
docker build -t flowcoder-eval:py311 -f scripts/Dockerfile.eval .
python -m flowcoder.eval --image flowcoder-eval:py311      # 默认 50 题、并发 4
```

## 当前边界

- 温度未显式固定（client 无 temperature 接线），报告 meta 如实标注；P2b 一并处理。
- 与 evalplus 官方数字存在已知口径差：pass@1 分母 = 兼容轻量 harness 的题数。
