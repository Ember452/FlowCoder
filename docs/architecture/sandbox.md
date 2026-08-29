# 沙箱（sandbox/）

`src/flowcoder/sandbox/` 提供 Docker 容器级执行隔离。P1a 只做单容器执行与资源限额；
池化与泄漏回收（`pool.py` / `reaper.py`）属 P1b。

## 模块结构

| 模块 | 职责 |
|---|---|
| `runtime.py` | `ContainerRuntime` Protocol（对 Docker SDK 的最小抽象，6 个操作）+ `DockerRuntime` 实现。SDK 是同步阻塞库，所有调用经 `asyncio.to_thread` 进入事件循环 |
| `container.py` | `SandboxContainer`：单容器生命周期（start / execute / close）与双层超时 |
| `limits.py` | 容器级 cgroup 限额（`--memory` / `--cpus` / `pids-limit`）→ docker create 参数翻译 |
| `network.py` | 网络策略：默认 `network_mode=none`（除 loopback 外无任何接口） |
| `transport.py` | 文件进出：内存 tar + `put_archive`（docker cp 等价物），相对路径校验 |

## 一次执行的流程

```
SandboxContainer.start()
  → runtime.create(spec)    # read_only 根 FS、tmpfs 工作目录与 /tmp、non-root、断网、限额
  → runtime.start(cid)
SandboxContainer.execute(cmd, files=?, timeout_s)
  → transport.copy_files()  # 可选：内存 tar 传入工作目录
  → 内层：容器内 `timeout --kill-after G T sh -c <cmd>`（TERM→宽限→KILL）
  → 外层：asyncio.wait_for(exec, T+G+EXEC_MARGIN_S)，超时对容器 TERM→KILL 兜底
  → ExecutionResult(exit_code, stdout, stderr, duration_ms, timed_out)
SandboxContainer.close()     # remove(force=True)，幂等
```

## 关键设计决策（详见 docs/specs/2026-08-29-sandbox-p1a-adr.md）

1. **容器级 + 执行级双层限额**：cgroup 限额防资源耗尽（不可绕过），执行级超时防单次挂死（快速回收）。任一层失效另一层兜底。
2. **断网是默认值**：`network_mode=none` 是 Docker 强隔离，不存在"忘了关网"的配置疏漏；需要联网的执行必须显式声明 `network_enabled=True`。
3. **不用目录挂载**：文件经内存 tar + `put_archive` 传入（docker cp 等价），规避 WSL2 跨文件系统慢 IO，同时杜绝宿主路径暴露给容器。
4. **只读根 + tmpfs 白名单**：根文件系统只读，可写仅 `/workspace`（工作目录）与 `/tmp` 两个 tmpfs；容器内进程无法篡改镜像内容或写入宿主。
5. **fake 运行时测试策略**：`ContainerRuntime` Protocol 使单元测试注入 fake（不依赖真实 Docker，既定决策）；真实容器的集成测试挂 `docker` marker 自动跳过。

## 与其他模块的关系

- **permissions/（P1c 接入）**：工具调用先过权限四模式审批门，再进沙箱执行——权限门在沙箱之前。
- **observability**：`ExecutionResult` 携带耗时与退出码，P1b 接入 TraceManager。

## 当前边界（P1a 不做）

- 无池化：每次执行一个容器实例，冷启动成本由 P1b 的 `SandboxPool` 消除。
- 白名单域名代理未实现，本阶段只有全断 / 全开两档。
