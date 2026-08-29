# 沙箱（sandbox/）

`src/flowcoder/sandbox/` 提供 Docker 容器级执行隔离。P1a 完成单容器执行与资源
限额，P1b 补齐容器池化（`pool.py`）、泄漏回收（`reaper.py`）与指标（`metrics.py`）。

## 模块结构

| 模块 | 职责 |
|---|---|
| `runtime.py` | `ContainerRuntime` Protocol（对 Docker SDK 的最小抽象，9 个操作）+ `DockerRuntime` 实现。SDK 是同步阻塞库，所有调用经 `asyncio.to_thread` 进入事件循环 |
| `container.py` | `SandboxContainer`：单容器生命周期（start / execute / close）与双层超时 |
| `limits.py` | 容器级 cgroup 限额（`--memory` / `--cpus` / `pids-limit`）→ docker create 参数翻译 |
| `network.py` | 网络策略：默认 `network_mode=none`（除 loopback 外无任何接口） |
| `transport.py` | 文件进出：内存 tar + `put_archive`（docker cp 等价物），相对路径校验 |
| `pool.py` | `SandboxPool`：预热 N 容器、O(1) 租借、归还即销毁重建、Condition 排队背压（max_queue 上限）、租借健康体检 |
| `reaper.py` | `LeaseReaper`：心跳对账租借台账，归属任务已结束的孤儿容器强杀；池启动时按 label 清扫遗留容器 |
| `metrics.py` | `SandboxMetrics`：租借等待/复用次数/执行耗时/资源峰值的聚合指标 |

## 一次执行的流程

```
SandboxPool.start()
  → 按 flowcoder.sandbox label 清扫上次运行遗留容器
  → 并发预热 N 个 SandboxContainer（带 label 与安全默认 spec）
SandboxPool.execute(cmd) / lease() → Lease.execute() → Lease.release()
  → 租借：idle 弹出（池空则 Condition 排队，超 max_queue 抛 PoolExhaustedError）
  → 体检：is_alive，死容器销毁补建并改取下一个
  → 执行：transport 传文件（可选）→ 双层超时执行 → 采样 stats 记资源峰值
  → 归还：销毁容器，后台补建同规格容器回填池
LeaseReaper.run_forever()
  → 周期对账租借台账：归属任务已结束未归还的容器强杀并补建
```

单容器模式（不经池）：

```
SandboxContainer.start()
  → runtime.create(spec)    # read_only 根 FS、tmpfs 工作目录与 /tmp、non-root、断网、限额
  → runtime.start(cid)
SandboxContainer.execute(cmd, files=?, timeout_s)
  → 内层：容器内 `timeout --kill-after G T sh -c <cmd>`（TERM→宽限→KILL）
  → 外层：asyncio.wait_for(exec, T+G+EXEC_MARGIN_S)，超时对容器 TERM→KILL 兜底
  → ExecutionResult(exit_code, stdout, stderr, duration_ms, timed_out)
SandboxContainer.close()     # remove(force=True)，幂等
```

## 关键设计决策（详见 docs/specs/ 两篇 ADR）

1. **容器级 + 执行级双层限额**：cgroup 限额防资源耗尽（不可绕过），执行级超时防单次挂死（快速回收）。任一层失效另一层兜底。
2. **断网是默认值**：`network_mode=none` 是 Docker 强隔离，不存在"忘了关网"的配置疏漏；需要联网的执行必须显式声明 `network_enabled=True`。
3. **不用目录挂载**：文件经内存 tar + `put_archive` 传入（docker cp 等价），规避 WSL2 跨文件系统慢 IO，同时杜绝宿主路径暴露给容器。
4. **只读根 + tmpfs 白名单**：根文件系统只读，可写仅 `/workspace`（工作目录）与 `/tmp` 两个 tmpfs；容器内进程无法篡改镜像内容或写入宿主。
5. **fake 运行时测试策略**：`ContainerRuntime` Protocol 使单元测试注入 fake（不依赖真实 Docker，既定决策）；真实容器的集成测试挂 `docker` marker 自动跳过。
6. **归还即销毁重建**（P1b）：跨租借不复用容器，杜绝脏容器状态泄漏；复用只发生在同一次租借内。预热消除冷启动，补建在后台进行。
7. **背压 + 快速失败分层**（P1b）：池空排队等待平滑突发，等待者超 max_queue 抛 PoolExhaustedError 防雪崩。
8. **TraceSink 鸭子类型**（P1b）：池经 create/update/complete 三方法协议接入 TraceManager，sandbox 不反向依赖 agents。

## 与其他模块的关系

- **permissions/（P1c 接入）**：工具调用先过权限四模式审批门，再进沙箱执行——权限门在沙箱之前。
- **observability**：`ExecutionResult` 携带耗时与退出码，P1b 接入 TraceManager。

## 当前边界

- 白名单域名代理未实现，本阶段只有全断 / 全开两档。
- 真实容器压测/演示数据（20 并发无泄漏、冷启动消除对比）待 Docker 环境就绪，
  清单见两篇 ADR 文末。
