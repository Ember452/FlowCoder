# FlowCoder 代码质量审查报告

> 审查范围：src/flowcoder/ 全部源码（agent/teams、app/commands/config/daemon、providers/client/mcp、context/memory/permissions/tools/hooks/skills）
> 审查方式：四路并行深读，所有问题均带 file:line 证据；沙箱绕过已用脚本实测复现。

## 总评

架构分层意图清晰、注释质量高、测试比例健康，但存在三类系统性问题：**① 安全面有真洞**（沙箱绕过已复现、fork 技能零权限、hook 命令注入）；**② 异常路径挂死**（三个 P0 都是"正常路径能跑、异常路径挂死"形态）；**③ 复制后各自演进**（三套 LLM 协议解析、app.py 上帝文件、两条 Agent 执行路径行为漂移）。另有大量 asyncio 卫生欠账与静默数据丢失路径。**结论：先修 P0，再启动 TRANSFORMATION_PLAN。**

---

## P0 — 必须立即修（12 项）

> 2026-08-29 更新：**12 项已全部修复**，每项带回归测试；安全类的根因与修法详见 `docs/specs/2026-08-29-p0-security-fixes.md`。标注格式：✅ 已修复 — 回归测试。

### 安全类

| # | 位置 | 问题 | 状态 |
|---|---|---|---|
| 1 | `permissions/sandbox.py:36-52` | **沙箱绕过（已实测复现）**：目标不存在时 fallback 分支的 `..` 未消除，`relative_to` 是纯词法匹配。`<root>/x/../../secret.txt`（x 不存在）通过校验，实际读写落在沙箱外。修法：fallback 后对完整 real_path 再 `resolve(strict=False)` 重查 | ✅ 已修复 — fallback 后对完整路径 `resolve(strict=False)` 重查（Windows 上该分支因 Win32 `..` 词法折叠语义不可达，POSIX/CI 回归）；测试：`test_permissions.py::TestPathSandbox::test_nonexistent_target_with_dotdot_escape_denied`、`test_multiple_dotdot_escape_to_sibling_denied`、`test_windows_drive_case_normalized` |
| 2 | `skills/executor.py:101-109` | fork 技能传 `permission_checker=None`，且 `allowed_tools` 为空时 `filter_tool_registry` 原样返回完整 registry——fork 模式可无审批执行任意工具（含 Bash），完全绕过 permissions 层 | ✅ 已修复 — `_AskDenyingChecker`（继承父 checker 规则/模式，ask→deny，与 noninteractive 语义对齐）；测试：`test_executor.py::test_fork_cannot_execute_ask_tools_without_approval` |
| 3 | `hooks/models.py:112-134` + `hooks/executors.py:40-46` | **Hook 命令注入**：`$FILE_PATH`/`$TOOL_ARGS.*` 原文替换进 `create_subprocess_shell`。文件名含 `; rm -rf ~` 即注入。展开值必须 `shlex.quote` | ✅ 已修复 — `expand(shell_quote=True)` + 平台感知引用（POSIX shlex.quote / Windows `^` 转义，cmd 不认单引号）；测试：`test_executors.py::test_expand_shell_quote_wraps_values`、`test_command_action_neutralizes_shell_injection` |

### 挂死/崩溃类

| # | 位置 | 问题 | 状态 |
|---|---|---|---|
| 4 | `providers/openai_responses_request.py:25` | Responses API 的 tools 格式未转换（透传内部 schema，该 API 要求扁平格式），带工具的请求必然 400。Chat Completions 有转换函数，Responses 漏了——三协议漂移的典型 | ✅ 已修复 — 新增 `convert_tools_for_responses`（对齐 compat 实现，扁平格式原样透传）；测试：`test_openai_responses_request.py::test_build_openai_response_request_kwargs_converts_internal_tool_schema` |
| 5 | `providers/openai 流式 core.py:320-324` | StreamEnd 只在 usage chunk 里发；vLLM/Ollama 等无 usage chunk 或中途断流时循环退出但无终止事件，上层无限等待 | ✅ 已修复 — 协议无关的 `providers/_stream_common.py::with_guaranteed_stream_end` 收尾包装（断流/无 usage chunk/重复 StreamEnd 三种路径保证恰好一个终止事件）；测试：`test_stream_finale.py::test_compat_without_usage_chunk_still_yields_single_stream_end`、`test_compat_broken_stream_still_yields_single_stream_end` 等 |
| 6 | `providers/openai_responses core.py:225-265` | `response.failed`/`incomplete`/`error` 事件被白名单静默丢弃，失败时不报错也不发 StreamEnd，同样挂死 | ✅ 已修复 — failed/error → `StreamEnd(stop_reason="error")`，incomplete → `stop_reason="max_tokens"`（供 output_recovery 续写），并接入收尾包装器；测试：`test_stream_finale.py::test_responses_failed_event_yields_error_stream_end`、`test_responses_incomplete_event_yields_max_tokens_stream_end`、`test_responses_broken_stream_still_yields_single_stream_end` |
| 7 | `app.py:1169-1171` | `adopt_running(self._subagent_task, ...)` 把 asyncio.Task 当 Agent 传入，触发即 AttributeError；且 `_subagent_task` 从未赋值，分支永假——既是 bug 又是死代码 | ✅ 已修复 — 删除死分支与 `_subagent_task` 属性；验证：全量测试绿 + grep 无残留引用（死代码删除，无可观察行为的回归测试） |
| 8 | `app.py:951-954 vs 1488/1509` | 发消息竞态：用户输入与通知触发的两条 `_send_message` 可并发执行（`_streaming` 布尔在 create_task 后才生效），conversation 历史交错。需单一队列/锁 | ✅ 已修复 — `SerializedSendGate`（单一 asyncio.Queue + 单一泵任务，四个触发源全部改走 submit；cancel 同时丢弃排队请求）；测试：`test_app.py::test_gate_serializes_concurrent_submissions`、`test_gate_cancel_drops_queued_requests`、`test_gate_pump_restarts_after_completion` |

### 引擎类

| # | 位置 | 问题 | 状态 |
|---|---|---|---|
| 9 | `agent/core.py:489-498` | fire-and-forget 任务无引用（可能被 GC 中途回收）、无异常兜底日志。应保存到 `self._bg_tasks` 集合 | ✅ 已修复 — `Agent._spawn_bg`（`_bg_tasks` 集合持引用 + done callback 记 error 日志）；测试：`test_agent_bg_tasks.py::test_bg_task_keeps_reference_and_cleans_up`、`test_bg_task_exception_is_logged`、`test_bg_task_cancellation_is_not_logged_as_error` |
| 10 | `agent/core.py:578 vs 626-629` | 并行路径无条件清零 `consecutive_unknown`，串行路径的 unknown 工具熔断（≥3 终止）在可并行分区时完全失效 | ✅ 已修复 — 移除并行批的无条件清零（计数只由串行路径更新：unknown +1、已知串行执行清零）；测试：`test_agent.py::test_stop_consecutive_unknown_tools_with_parallel_reads` |
| 11 | `agent/tool_authorization.py:72` | PermissionRequest 的 `await future` 无超时（对比 AskUser 有 300s），前端失联时 Agent 永久挂死 | ✅ 已修复 — `PERMISSION_REQUEST_TIMEOUT=300` + `asyncio.wait_for`，超时按拒绝收尾；测试：`test_agent_tool_authorization.py::test_authorize_tool_call_times_out_when_frontend_silent` |
| 12 | `context/manager.py:353` | `if "prompt" in err and "long" in err or "too many" in err:` —— and 优先于 or，任何含 "too many" 的错误都会触发静默丢弃最老 20% 轮次（有损且无记录） | ✅ 已修复 — 括号明确为 `("prompt" and "long") or ("too many" and "token")`，限流类错误不再触发有损丢弃；测试：`test_context.py::test_unrelated_too_many_error_does_not_drop_history`、`test_prompt_too_long_error_triggers_degrade_retries`、`test_too_many_tokens_error_triggers_degrade_retries` |

---

## P1 — 应该修（完整清单，22 项）

### asyncio 卫生

| # | 位置 | 问题 |
|---|---|---|
| 1 | `agent/recovery.py:33-34` | 事件循环内同步 `open().read()` 整个文件（在 core.py:692、789 的 async 工具执行路径上），大文件阻塞 loop；应 `asyncio.to_thread` 或限量读取 |
| 2 | `teams/manager.py:277-291` | `delete_team` 内同步 `subprocess.run(git ...)` 与 `shutil.rmtree`，TeamDeleteTool 路径会阻塞事件循环 |
| 3 | `app.py:924` + 1833-1894 | `_notification_check_task`（while True 轮询）退出清理从不取消，任务泄漏；2s 轮询本身偏重 |
| 4 | `app.py:898、1436、1440` | `asyncio.ensure_future` fire-and-forget：不保存引用（可能被 GC）、异常无人观察 |
| 5 | `app.py:1074、1822` | `except (asyncio.CancelledError, Exception): pass` 反模式，吞掉一切异常 |
| 6 | `teams/coordinator.py:9-38` | 用 `os.environ` 全局变量做会话模式开关，跨会话/多 Agent 互相污染 |

### 可靠性

| # | 位置 | 问题 |
|---|---|---|
| 7 | `daemon/server_state.py:178-186` + `session/store.py:91-97` + `records.py:73-78` | `_emit` 对每个事件同步执行 fsync 追加 + 全量 `serialize_conversation` + meta 原子重写，全部阻塞事件循环，高频流式时 daemon 卡顿；且 persist 失败时 `persisted_count` 不前移，恢复后同一批事件重复 append（store.py:202-204） |
| 8 | `daemon/routes/stream.py:151` | 每个 WS 客户端 20ms 忙轮询 `log_list`，无条件变量通知机制，N 客户端 = N 个空转循环 |
| 9 | `mcp/client.py:52、100、107` | initialize / list_tools / call_tool 全部无 timeout，卡死的 MCP server 永久阻塞 Agent 循环 |
| 10 | `mcp/manager.py:100-105` + `tool_wrapper.py:133、158-165` | 重连时 new 新 MCPClient 替换，旧 wrapper 仍持旧 client 并自行 reconnect，产生 manager 不追踪的孤儿连接；manager.py:79 重复 register_all_tools 时旧 client 未关闭直接覆盖（泄漏） |
| 11 | `mcp/client.py:34-58` + `manager.py:103` | connect 无并发锁，wrapper reconnect 与 manager get_client 并发时双开连接栈泄漏；manager.py:103 在 await 后重读 `self._configs[name]`，config 并发 reload 时 KeyError |
| 12 | `mcp/client.py:99-101` | list_tools 忽略分页，只取首页 `result.tools`，工具多的 server 静默丢工具 |
| 13 | `tools/bash.py:99-102` + `hooks/executors.py:51-53` | 超时 `proc.kill()` 只杀 shell 不杀子进程（需 POSIX `start_new_session`+`killpg` / Windows `taskkill /T`）；`ProcessLookupError` 未捕获；bash 输出无大小上限，大 stdout 直接进上下文 |

### 静默数据丢失

| # | 位置 | 问题 |
|---|---|---|
| 14 | `memory/auto_memory.py:213-223` | `_write_memories` 只保留 `- ` 开头条目并整体 `write_text` 覆盖，既有自由文本/未知标题静默丢失，且非原子写（崩溃即损坏） |
| 15 | `context/tool_results.py:60-63` + `manager.py:381` | `cleanup_tool_results` rmtree 全删，压缩后 keep_tail 和摘要里引用的 `<persisted-output>` 路径同时失效，模型 ReadFile 必失败 |
| 16 | `context/tool_results.py:67、72` | `session_dir / f"{tool_use_id}.txt"` 未消毒，tool_use_id 来自模型输出，含 `../` 可路径穿越；`FileExistsError: pass` 静默复用旧文件内容 |
| 17 | `memory/session.py:278-279、317-318` | resume/delete 将外部传入的 session_id 直接拼路径，未做 `[A-Za-z0-9_]` 校验（路径穿越） |

### 安全边界

| # | 位置 | 问题 |
|---|---|---|
| 18 | `permissions/dangerous.py:32` + `checker.py:93` | `find` 列入安全前缀，`find . -exec <任意命令>` / `-delete` 免确认放行；且 allow 判定发生在规则引擎（Layer 3）之前，用户 deny 规则（如 `Bash(find *)`）被绕过 |
| 19 | `permissions/checker.py:107` | Bash 属 command 类完全不受路径沙箱约束，`cat /etc/passwd` 免审批，与沙箱产品承诺矛盾（至少需文档/规则层显式声明） |
| 20 | `hooks/engine.py:146-151` | pre_tool_use hook 超时/异常返回 `success=False` 后仍无条件 reject——hook 失败被当作拒绝理由，应 fail-open 或区分失败类型 |

### 重复实现（漂移温床）

| # | 位置 | 问题 |
|---|---|---|
| 21 | `agent/core.py:743-787` vs `tool_execution.py:74-82` | `_execute_tool` 内联重写了 `execute_validated_tool` 的校验+异常处理，两路径行为易漂移；core.py:80 导入 `execute_validated_tool` 后从未使用（死导入） |
| 22 | `agent/core.py:868-1017` | `run_to_completion` 与 `run()` 大面积重复（compact/reinject/prepare_api_conversation/deferred reminder/执行循环），且缺 output_recovery、unknown 熔断、迭代上限报错；core.py:982/995 传 `thinking_blocks=[]` 丢弃 thinking 签名，Anthropic extended thinking + tool_use 连续轮次可能被 API 拒绝 |
| 23 | 配置默认值双份 | WorktreeConfig 默认值在 `config/core.py:126-128` 与 `validator.py:355-359` 各写一遍；MemoryConfig 默认 provider 在 core.py:144-146 与 validator.py:275-287 各一遍 |
| 24 | `daemon/routes/config.py:15-24` | 重复定义 `VALID_PROTOCOLS`/`VALID_PERMISSION_MODES`，未复用 config/validator.py，必然漂移 |

### 其他

| # | 位置 | 问题 |
|---|---|---|
| 25 | `daemon/server.py:182、132` | 两处独立读取 `FLOWCODER_DAEMON_TOKEN`；server.py:109-111 WS token 走 query string，会进代理/访问日志 |
| 26 | `config/core.py:84-91` | 魔法数 `128_000`、`64000`、`8192` 硬编码在 ProviderConfig 方法里，未进 validator/model_context 默认值体系 |
| 27 | `providers/anthropic_request.py:64` | thinking budget `max(max_output_tokens - 1, 1024)`：当 max_output_tokens≤1024 时 budget==max_tokens，违反 Anthropic "budget 严格小于 max_tokens" 要求，会 400 |
| 28 | `client/errors.py:50-57` | 解析了 retry-after，`RateLimitError.retry_after` 无任何消费者（重试体系只有分类没有重试）；errors.py:38-47 不支持 Retry-After 的 HTTP-date 格式 |

## P2 — 建议（完整清单）

### 死代码

- `tool_execution.py:129-174` `StreamingExecutor` 全仓库无实例化；core.py:75 死导入
- `agent/core.py:666-672` `Agent._consume_mailbox` 无调用方
- `events.py:42` `RetryEvent.wait` 从未赋值
- `teams/manager.py:10` 导入的 `BackendDetectionError` 未使用
- `backend_detect.py:31-36` `detect_backend` 忽略全部参数恒返 IN_PROCESS，与 `detect_pane_backend` 职责重叠且命名误导
- `app.py:1966` `_update_token_label` 空方法；:1396 UsageEvent 分支 `pass`；:634-636 `_current_streaming_label`/`_current_ai_row`/`_current_accumulated_text` 从未使用；`_send_message` 的 `is_notification` 形参未用
- `commands/registry.py:62-77` `asyncio.Lock` + async `register` 从未走 async 路径，实际全用 `register_sync`
- `context/manager.py:314` `messages_for_summary` 死代码；:16-51 大量未使用导入（`ensure_session_dir`、`append_replacement_records` 等）

### 重复逻辑

- `noninteractive_tools.py:33-45` `_tool_hook_context` 与 `tool_hooks.py:46-59` `build_tool_hook_context` 完全相同
- unknown/disabled 检查在 tool_execution.py、tool_authorization.py、noninteractive_tools.py 三处重复
- `context/manager.py:343` 与 :326 重复注入 SUMMARY_PROMPT（system+首条 user），白耗 token
- `anthropic_streaming.py:23-24` 与 `openai_streaming.py:62-63` 的参数 JSON 解析各写一份（且都吞错，见 P1）
- `client/__init__.py:11-18、31` 把 core 的私有别名（`_mark_last_tool_for_cache`、`_rate_limit_error`）再导出
- `skills/loader.py:29` `_cache` 与 `_skills` 重复状态

### 命名与风格

- `app.py:434-443` `_to_past_tense` 规则集产出 "Bootstraped"
- `permissions/modes.py:27-29` `PermissionMode.BYPASS` 与 `DONT_ASK` 决策矩阵完全相同
- `app.py:417-422` `_MODE_CYCLE` 不含 CUSTOM/DONT_ASK，配置成这两个值后 shift+tab 意外跳回 default
- `config/core.py:224` validator 输出键 `"class"` 映射到 `MemoryProviderConfig.class_name`，两套名字
- 配置键 `acceptEdits` camelCase 与其余 snake_case 不一致
- `core.py:77-78` 跨模块导入私有类型 `_AuthResult`/`_ToolExecResult` 并用于公共方法签名
- `helpers.py:16` `**kwargs: str | dict` 注解错误（值含 dict 且有 bool）
- `core.py:833` `manual_compact` 返回类型声明含 `ErrorEvent` 但实际不返回
- `teams/models.py:68` `_team_string_field` prefix="team" 被用于 teammate 字段，报错前缀误导
- `models.py:32`、progress.py 用 `Optional[]` 与他处 `X | None` 混用
- `hooks/events.py:2` docstring 写"12 种事件"，枚举实际 17 个成员
- `app.py:99` 顶层 `import re` 与 526 行函数内重复 import；693/908 重复 `os.getcwd()`
- `app.py:718、733、829` 等：`file_history`/`_exit_plan_tool`/`team_manager`/`_pre_plan_mode`/`_pending_perm_request`/`_pending_askuser_event`/`_exit_requested` 全部绕过 `__init__` 动态挂属性，导致 1485/1500/1657 处 hasattr 防御式检查

### 类型与数据边界

- teams/ 全部手写 dataclass + fields.py 手工校验，与全库 pydantic v2 风格割裂（SharedTask/MailboxMessage/AgentTeam 适合 pydantic）
- `usage.py:54` `usage_callback_payload` 与 spawn_inprocess `_on_event` 用裸 dict 跨边界 + 字符串事件名
- `Agent.file_history`/`_team_manager: Any`；`context_window.py:29` 直读 `config._fetched_context_window` 私有属性
- 跨模块私有访问：`app.py:751-752` 给 ExitPlanModeTool 注入私有 lambda；`app.py:1793` 读 `manager._clients`；`agent/task_manager.py:91` 读 `bg.agent._team_manager`；`skills/executor.py:127` 访问 `agent._conversation`；`spawn_inprocess.py:86` 访问 `progress._lock`
- `conversation.py:104` `last_input_tokens` 自认"仅为兼容"的僵尸字段

### 杂项缺陷与性能

- `factory.py:60` `config.providers[0]` 空列表直接 IndexError；:72 函数内 `import logging`
- `anthropic_streaming.py:45-60` redacted_thinking 块被静默丢弃
- `openai_streaming.py:160-164` ToolCallStart 可能重复或带空 id（`if name:` 不判已有值；id 晚于 name 到达时不再补发）
- `openai_streaming.py:226-233` `complete_from_summary` 已有 delta 后又带完整 summary 时重复推送整段 ThinkingDelta
- `openai_streaming.py:213` `complete_from_done_text` 的 endswith 启发式对分块文本可能误判致尾巴重复
- `core.py:97、198、290` 三个 SDK client 未配置任何 timeout（默认 600s），流无空闲/总超时兜底
- `permissions/rules.py:100` `evaluate` 每次工具调用重读三层 YAML（性能+TOCTOU）；fnmatch 在 Windows 大小写不敏感，规则行为跨平台不一致
- `skills/loader.py:99` `except (SkillParseError, Exception)` 冗余且吞掉一切
- `skills/directory.py:104-107` 同步 `self._impl(**kwargs)` 直接跑在事件循环里
- `memory/session.py:166` 每条消息同步重写 meta 文件
- `tools/bash.py:37` `rsplit("|")` 不识别引号内管道与 `||`，退出码语义映射误判
- `events.py:65` `TurnComplete` docstring 与实际产出时机（工具轮后也发）不符
- `memory.py:111-119` `extract_memories` 对内部已吞异常的 `observe` 再包一层 try/except 冗余

---

## 拆分建议（按"变化理由"而非行数）

### core.py（1030 行）—— run() 上帝方法的四路拆分

`run()` 混了 4 个变化理由，拆分边界：

| 现有内容（约行号） | 拆到哪 | 理由 |
|---|---|---|
| 压缩检查/编排（L325-376） | `agent/compaction/` 协调器 | 压缩策略独立演进（阈值、摘要、熔断），不该长在主循环里 |
| Plan 模式策略（L400-413） | permissions/ 模式层 | 是权限策略不是循环逻辑 |
| LLM 单轮执行：流式收集+用量+output_recovery（L441-478） | 抽成 `run_llm_turn()` 私有方法 | `run_to_completion`（L868-1017）与 `run()` 约 150 行重复的根源；抽出来两边复用，重复消失 |
| 工具分发：分区+预检+并行/串行+hook+结果装配（L521-650） | 独立 `ToolDispatcher` 类 | 工具分发是独立关注点；顺带修复"并行路径绕过 unknown 熔断"（P0-10）时必须动这块，一起拆 |
| AskUserQuestion 特例 + `assert isinstance`（L747-787） | 下沉到工具/Dispatcher 层 | 工具层逻辑越界进核心循环；assert 在 `-O` 下被剥除 |

### app.py（1967 行）—— 五类职责的完整去向

| 内容（约行号） | 去向 |
|---|---|
| ChatInput + 输入历史（145-306） | `ui/chat_input.py` |
| ToolCallBlock / ToolGroupSummary / SubAgentBlock、`_tool_title`、`_format_detail`、`COLLAPSIBLE_TOOLS`（308-550） | `ui/tool_blocks.py` |
| `scan_files_for_at` / `expand_at_refs`（104-142，注意 P0-2 的裸 open 一起修） | `ui/at_refs.py` |
| SPINNER_FRAMES、THINKING_VERBS、`_to_past_tense`（重写规则表）、`_FLOWCODER_THEME`（431-560） | `ui/spinner.py` / `ui/theme.py` |
| `_send_message` 事件分发主循环（1223-1468）、`_mount_live`/`_show_error`/`_show_system_message`（1896-1937）、spinner/teammate 定时器（1603-1691） | `app/stream_view.py`（ChatStreamController，对 App 持弱引用） |
| `_select_provider` 的工具/技能/团队/worktree/命令注册（685-926） | `app/bootstrap.py`（装配函数 `wire_runtime(app, provider)`，消灭 720-733 的动态属性注入与全部 hasattr 防御） |
| `_handle_permission_request`、`_handle_askuser_event`、`_show_plan_approval` + 三个 `on_*_responded`（1513-1727） | `ui/inline_dialogs.py` |
| `_process_task_notifications` / `_start_notification_polling` / `_process_mailbox_notifications`（1472-1511） | `app/notifications.py`（同时修 P1 的任务泄漏：退出时 cancel） |
| `_prefetch_relevant_memories`（1179-1221） | `memory/prefetch.py` |
| `_init_mcp` / `_shutdown_mcp` / `_resolve_context_window`（928-937、1779-1827） | `app/mcp_controller.py` |
| `_cleanup`、`_update_session_summary`、`_persist_compact_boundary`（1005-1018、1760-1894） | `app/lifecycle.py` |

拆完 app.py 约剩 300 行壳。动态属性（`file_history`、`team_manager`、`_exit_plan_tool` 等在 `__init__` 外挂的）随 bootstrap 拆分一并收编进 `__init__`。

### providers/ —— 下沉四个"写了两三份"的公共组件

| 重复逻辑 | 现状 | 去向 |
|---|---|---|
| 流收尾保证（StreamEnd 恰好一个） | 三种协议三种写法，两处异常路径挂死（P0-5/6） | `providers/_stream_common.py` 协议无关的 StreamFinale 装饰器 |
| tools 格式适配 | 仅 openai_compat 有，Responses 缺（P0-4） | `providers/_tool_convert.py` 单一实现 |
| `parse_tool_arguments`（坏 JSON 吞成 `{}`） | anthropic/openai 各一份 | 下沉并改为"报错而非吞错" |
| finish_reason → stop_reason 映射 | compat 只认两种，anthropic 忠实透传 | `providers/_finish_reason.py` 统一映射表 |

### 其他模块

- **context/manager.py**：承担阈值计算/提示词/重试/熔断/编排五件事 → 抽 `Summarizer` 接口与 `KeepPolicy`（压缩策略可插拔），`apply_tool_result_budget` 178 行长函数拆分
- **memory/**：`MemoryManager`（auto_memory）绕过 provider 抽象直写 markdown，与 `providers/hub.py` 两套并行 → 收敛到 MemoryProvider 单一体系
- **teams/**：手写 dataclass 校验 → 迁 pydantic v2，与全库风格统一（放 R3）

### 拆分时机（防改两遍）

core.py 四路拆分与 providers 公共组件下沉安排在 **R2（五端口抽取）** 一并进行——两者都是"把机制从策略里分出来"，一起做避免重复触碰同一批文件；app.py 拆分安排在 **R3**。P0.5 只修 bug 不拆结构。

---

## CI 基线与既有测试失败

2026-08-29 建立 lint/format 门禁时实测：全仓 `ruff check` 修复 164 项（135 项自动）、211 个文件统一格式化后，单元测试结果与 HEAD 基线完全一致——**10 failed / 1021 passed / 2 skipped**，证明格式化与 lint 修复零行为变化。

既有 10 个失败与本次改动无关，主要是 mcp SDK 版本漂移（`Tool.inputSchema` 属性名不匹配，`tests/unit/mcp/test_mcp.py::TestMCPManagerPartialFailure` 两个用例等），并入 P0.5 / Phase 3 的 MCP 加固一并修复。

另发现：原 CI 配置的 lint 步骤从未真正通过过（dev 依赖缺 ruff，步骤会直接 command not found；`release.yml` 缺 `build` 包），已修复。

## 修复顺序建议

1. **批次一（安全+挂死，P0 全部 12 项）**——在任何新功能开发之前，每项补回归测试
2. **批次二（漂移收敛 + asyncio 卫生，P1 中的重复实现与 asyncio 项）**——为 P1/P2 改造铺路
3. **其余 P1**（daemon 性能、config 单一来源、MCP 加固）并入 TRANSFORMATION_PLAN Phase 3
4. **P2 与 app.py 拆分**并入 FRAMEWORK_REFACTOR_PLAN（R3），避免改两遍
5. 全部 P0 修完前，不启动沙箱/评测开发（沙箱安全建立在 sandbox.py 正确性之上）
