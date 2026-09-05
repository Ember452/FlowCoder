# 质量加固计划（2026-09 大厂对标审查）

> 背景：在不启动框架重构（FRAMEWORK_REFACTOR_PLAN）的前提下，按"大厂高质量项目"标准对代码质量、测试体系、安全与运维就绪度做三路并行审查（每条结论带 file:line 证据），并落地其中无需架构改动的修复项。本文档记录已修复项与遗留项，供后续迭代跟进。

## 一、已修复（2026-09）

### 1. 密钥外泄通道（安全，最高优先级）

| 问题 | 修法 |
|---|---|
| `permissions/dangerous.py` 把 `env`/`printenv` 列为免审批安全命令，Agent 可直接读出环境变量中的 API key | 从 `_SAFE_EXACT_COMMANDS` 移除，转入正常审批流 |
| `cat`/`head`/`tail`/`grep` 等读文件命令免审批，可读 `~/.flowcoder/config.yaml`（明文 api_key）、`.ssh/id_rsa` 等 | 新增 `_SENSITIVE_PATH_MARKERS`：参数指向凭据类路径（`.flowcoder`/`.ssh`/`.aws`/`.kube`/`.netrc`/`id_rsa` 等）时不再免审批，转人工确认 |
| Bash 经 `create_subprocess_shell` 执行，`echo $ANTHROPIC_API_KEY` 会在运行期展开变量泄漏密钥 | 安全命令含 `$` 一律转审批（白名单命令均为无需变量的只读操作） |
| `config/core.py::update_user_config_value` 裸 `write_text` 落盘含密钥的 config.yaml，POSIX 上默认组/其他用户可读 | 改为 mkstemp（0o600）+ 原子替换，与 `account/session.py`、daemon 路由的既有写法对齐 |

回归测试：`test_permissions.py::test_secret_leaking_commands_are_not_safe`、`::test_normal_file_reading_is_still_safe`。

### 2. 事件循环阻塞（teams/worktree 工具链）

async 函数内直接跑同步 `subprocess.run`（git timeout 60s、tmux 10s）与同步文件 IO，违反 AGENTS.md 异步纪律。核心链路（client/sandbox/hooks）本是干净的，问题集中在边缘模块：

- `worktree/manager.py`：`create`/`enter`/`exit`/`_remove_worktree`/`auto_cleanup` 内的 git 子进程与 `count_worktree_changes`/`has_worktree_changes`/`perform_post_creation_setup` 全部 `asyncio.to_thread` 化；删除用 `asyncio.sleep(0.1)` 掩盖竞态的写法（to_thread 顺序 await 本身已保证落盘次序）
- `tools/agent/tool.py`：`_spawn_pane_teammate`（tmux/iTerm2 子进程）to_thread
- `tools/send_message.py`：`_wake_pane` 改 async + to_thread，唤醒失败不再静默（debug 日志可观测）
- `tools/team_delete.py`：`delete_team`（含 tmux + rmtree）to_thread
- `commands/handlers/clear.py`、`commands/handlers/session.py`：`SessionManager.create/resume`（同步文件 IO，resume 需全量解析 jsonl）to_thread

### 3. CancelledError 取消语义统一

此前 client/resilience 层严格执行"取消必须放行"，另有 9 处吞掉不 re-raise，标准分裂。统一为两类模式：

- **等待被取消的子任务**：改用 `asyncio.gather(task, return_exceptions=True)`——吞掉子任务自身的取消，但当前协程被取消时 CancelledError 仍会放行。涉及 `app.py`（中断/退出两处）、`daemon/background.py`（lifespan 收尾）、`daemon/outbox.py`、`daemon/routes/stream.py`
- **任务边界**：标记状态/发完事件后 re-raise。涉及 `agents/task_manager.py`（两处）、`daemon/tasks/runner.py`
- **例外**：`teams/spawn_inprocess.py::result` 读取的是已结束子任务的终态，捕获 CancelledError 属正确语义，补注释保留

### 4. 资源清理

- `tools/bash.py` 新增公共 `aclose()`；`app.py::_cleanup` 退出时关闭 Bash 沙箱池，修复"预热容器（sleep infinity）在 TUI 退出后残留"
- `sandbox/pool.py::start()` 此前已实现按 label 清扫遗留容器，确认无需重复建设

### 5. 工程门禁（CI/供应链）

- **e2e 进 CI**：`tests/e2e` 全部基于 fake/进程内 TestClient（除 docker 标记用例自带 skipif），纳入 test job
- **覆盖率门禁**：`pyproject.toml [tool.coverage.report] fail_under=62`（2026-09 实测基线 63%，只防退化，提升后应上调）
- **pip-audit**：CI 新增 audit job，依赖漏洞扫描
- **release 权限收窄**：workflow 级默认 `contents: read`，写权限下沉到 job 级
- **uv.lock**：提交 lockfile，构建可复现

## 二、遗留项（需决策或依赖架构配套，暂缓）

| 项 | 现状 | 暂缓原因 / 建议 |
|---|---|---|
| LeaseReaper 接线 | `sandbox/reaper.py` 已实现但无调用点 | 根因不是没接线，而是 Bash 工具的池租借不带 `task_id`（reaper 对无归属租借一律跳过，接了也空转）。需要 daemon 级共享池 + task_id 贯通，属框架重构范围；届时一并接入 lifespan |
| 危险命令检测黑名单面窄 | 8 条正则（`rm -rf /`、fork bomb 等），`rm -rf ~` 等变体不命中 | 更系统的修法是命令解析级白名单或默认沙箱化（sandbox_mode 默认 docker），动权限模型，需单独设计 |
| 路径沙箱 TOCTOU | `resolve()` 校验与实际打开之间符号链接可被替换 | 需要 open-time（`O_NOFOLLOW`）级校验，改文件工具打开路径，收益/复杂度比待评估 |
| app.py 单测真空 | 2111 行仅 3 个单测 | TUI 组件测试成本高；已有 e2e 关键路径覆盖兜底，建议随框架重构拆 app.py 时同步补 |
| mypy/pyright 静态类型门禁 | 注解覆盖率 98.8% 但无校验，10 处 `type: ignore[assignment]` 同源于 `Tool.execute(params)` 未泛型化 | 引入 pyright basic 需先清存量报错；建议与 `Tool[P: BaseModel]` 泛型化一起做 |
| WS token 走 query string | token 可能进访问日志 | 改 header/首帧认证需要同步改前端重连逻辑，单独排期 |
| 结构化日志 + trace_id 贯穿 | trace_id 出不了进程，日志无结构化字段 | 需统一 logging filter/formatter 并注入 LLM HTTP 头，涉及全部模块的日志调用面 |
| 镜像源 | pyproject 默认 index 指向阿里云镜像 | 属开发者本地环境取舍（uv.lock 已固化解析结果），保留现状，团队协作时再议 |

## 三、审查确认的优势（保持即可）

- Docker 沙箱：默认断网、256MB/1CPU/pids128 限额、只读根 + 非 root、双层超时、无 privileged/sock 挂载
- daemon：默认 loopback 绑定、非 loopback 强制 token（`hmac.compare_digest`）、OriginGuard + CORS 白名单
- 密钥：服务端不回传 key 明文（只给 `api_key_set` 布尔位）、account.yaml 经 mkstemp 0o600、无硬编码密钥
- 测试：1435 个用例零网络零越界、断言精确到事件序列、混沌演练脚本化
- 代码卫生：裸 except 0 处、`raise from` 44 处、TODO 全仓 1 处、print 不出 CLI 入口
