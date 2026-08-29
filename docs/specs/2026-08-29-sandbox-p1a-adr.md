# ADR：沙箱单容器执行与资源限额（P1a）

- 状态：Accepted（真实容器验收项延后，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P1a、TRANSFORMATION_PLAN.md Phase 1、docs/architecture/sandbox.md

## 背景与目标

FlowCoder 的 bash 工具目前裸 subprocess 执行，无资源限额与隔离。P1a 新建
`src/flowcoder/sandbox/`，提供单容器执行：任意代码/命令在受限容器内运行并回传
stdout/stderr/退出码/耗时。约束：开发环境无 Docker（既定决策），沙箱逻辑必须
能被纯 fake 的单元测试全量验证。

## 决策与理由

### D1：容器级 + 执行级双层限额

- **容器级**（`limits.py`）：docker create 时设 `mem_limit` / `nano_cpus` /
  `pids_limit`。基于 cgroup，容器内进程无法绕过，防 fork 炸弹与内存耗尽。
- **执行级**（`container.py`）：双层超时——
  - 内层：容器内 `timeout --kill-after <G> <T>`，进程超时先收 SIGTERM，宽限 G
    后补 SIGKILL，给优雅退出的机会；
  - 外层：`asyncio.wait_for(exec, T + G + EXEC_MARGIN_S)`。若 exec 自身挂死
    （docker daemon 无响应、容器假死），内层永远不会返回，外层对**容器**执行
    TERM→KILL 兜底。EXEC_MARGIN_S=5s 覆盖 docker exec 自身开销。
- 为什么两层都要：cgroup 限额管"资源总量"但不管"单次执行拖多久"；内层 timeout
  管单次执行但如果 exec 通道本身挂死则失效；外层是最终兜底。任一层失效另一层
  仍能收场，"无残留容器"由 `close()` 的 `remove(force=True)` 幂等保证。

### D2：断网是默认值

`network_mode=none` 是 Docker 的强隔离：容器内除 loopback 外没有任何网络接口，
不是防火墙规则（可被容器内 root 改动）而是接口层面不存在。沙箱执行的是不可信
代码，默认联网意味着任意数据外传与内网探测。需要联网的执行必须显式
`SandboxConfig(network_enabled=True)`，让意图出现在调用方代码里。
白名单域名代理（TRANSFORMATION_PLAN 的后续能力）在此默认之上按需放开。

### D3：文件传输用内存 tar + put_archive，不用目录挂载/临时卷

- 目录挂载把宿主路径直接暴露给容器内进程，是攻击面；
- WSL2 跨文件系统（Windows NTFS ↔ Linux ext4）IO 慢是已知痛点，挂载目录读写
  性能差一到两个数量级；
- `put_archive`（docker cp 等价）一次内存 tar 传入，无宿主路径暴露、无跨 FS IO。
  `transport.py` 对路径做白名单校验：拒绝绝对路径、`..` 穿越、反斜杠
  （POSIX 合法但极易造成 Windows 语义混淆），多级路径自动补父目录条目。

### D4：安全默认的组合

`python:3.11-slim` + non-root（65534:65534）+ `read_only=True` 根文件系统 +
tmpfs 仅 `/workspace`（mode=1777）与 `/tmp` + 断网 + 三维 cgroup 限额。
单条默认被绕过时其余仍构成纵深。

### D5：ContainerRuntime Protocol 与 fake 测试策略

Docker SDK 是同步阻塞库，抽象为 6 个操作的 Protocol（create/start/put_archive/
exec_run/kill/remove），真实实现 `DockerRuntime` 每个调用经 `asyncio.to_thread`
进事件循环（异步纪律）。单元测试注入 `FakeRuntime`（记录调用、注入 exec 行为），
覆盖：正常执行、超时击杀（外层兜底 TERM→KILL）、限额参数翻译、断网默认、路径
穿越拒绝、SDK 缺失/daemon 不可用的错误路径。真实容器场景挂 `docker` marker 集成
测试，无 daemon 自动 skip。

### D6：docker SDK 为可选依赖

`pyproject.toml` 声明 `[project.optional-dependencies] sandbox = ["docker>=7.0"]`。
不装 SDK 时 `DockerRuntime.from_env()` 抛出带安装指引的 `SandboxError`，核心包
零依赖增长（符合项目极简定位）。P1c 的 `sandbox_mode` 配置将在此之上做降级提示。

## 后果

- 单容器模型下每次执行都有冷启动成本 → P1b 的 `SandboxPool` 预热租借消除。
- 真实容器验收项（超时击杀实测、内存超限 OOM、断网 curl 失败、kill -9 无残留）
  由 `tests/integration/sandbox/test_container_real.py` 承载，本机无 Docker 显示
  skip；待环境就绪后实测，数据回填本 ADR。

## 真实容器验收回填（待 Docker 环境就绪）

- [ ] test_normal_execution / test_python_script_from_copied_file 实测通过
- [ ] test_timeout_killed 实测（预期退出码 124/137）
- [ ] test_memory_limit_kills_hog 实测（预期非零退出）
- [ ] test_network_isolated_by_default 实测（预期连接失败）
- [ ] test_file_cannot_escape_workdir 实测（预期只读根拒绝写入）
- [ ] kill -9 容器后无残留（docker ps 对账）
