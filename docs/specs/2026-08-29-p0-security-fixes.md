# ADR：P0.5 安全修复批次——三个安全洞的根因与修法

> 日期：2026-08-29 · 范围：CODE_QUALITY_AUDIT.md P0-1/2/3（安全类）
> 修复原则：最小 diff、先写失败测试再修、不顺手拆结构（结构问题留给 R2/R3）。

## ADR-001：PathSandbox fallback 分支的 `..` 穿越绕过（P0-1）

### 根因

`PathSandbox.check()`（permissions/sandbox.py）对**不存在**的目标走 fallback 分支：
沿 `.parent` 链向上找到第一个存在的祖先，resolve 后把"不存在的尾部"拼回去：

```python
real_path = resolved_ancestor / abs_path.relative_to(ancestor)
```

`abs_path` 未经词法归一化（Python 3.13 的 `Path.absolute()` 不消除 `..`），
`relative_to` 拼回的尾部可能携带 `..` 段；而最终的 `relative_to(root)` 白名单校验
是**纯词法匹配**，不消除也不检查 `..`。于是
`<root>/x/../../secret.txt`（x 不存在）拼回后是 `<root>/x/../../secret.txt`，
词法上以 root 开头 → 放行；实际 stat 落在 root 之外 → 穿越成功。

### 修法

fallback 拼接完成后，对**完整 real_path** 再 `resolve(strict=False)` 重查：

```python
real_path = (resolved_ancestor / abs_path.relative_to(ancestor)).resolve(strict=False)
```

`resolve` 会消除 `..` 段并解析既有部分的符号链接，之后 `relative_to(root)`
的词法匹配才与真实位置一致。`strict=False` 允许目标不存在（这正是 fallback
分支的场景）。

### 为什么 Windows 上测不出红（平台语义差异，重要）

实测（Python 3.13.7 / Windows）：Win32 的 stat/NormPath 语义会**词法折叠 `..`
而不要求中间组件存在**，因此含 `..` 的路径在 Windows 上 `resolve(strict=True)`
照样成功，带 `..` 的输入根本进不了 fallback 分支——漏洞在 Windows 上不可达，
回归测试在本机双绿（修复前后都通过）。该漏洞在 POSIX 语义下真实可达
（CI 的 ubuntu runner 会执行该路径，修复前回归测试失败、修复后通过）。
测试保留并在两个平台运行；结论以 CI 为准。

### 回归测试

`tests/unit/permissions/test_permissions.py::TestPathSandbox`
- `test_nonexistent_target_with_dotdot_escape_denied`（逃逸目标刻意选在系统临时目录之外——临时目录本身在沙箱白名单里）
- `test_multiple_dotdot_escape_to_sibling_denied`
- `test_windows_drive_case_normalized`（盘符大小写归一化：`C:` vs `c:` 不影响判定）

## ADR-002：fork 技能绕过 permissions 层（P0-2）

### 根因

`SkillExecutor.execute_fork`（skills/executor.py）构造 fork Agent 时传
`permission_checker=None`，且 `allowed_tools` 为空时 `filter_tool_registry`
原样返回完整 registry。fork Agent 的交互循环在授权时发现 checker 为 None
直接放行——fork 模式可以无审批执行任意工具（含 Bash），完全绕过 permissions 层。

为什么不直接透传父 checker：fork 是**非交互**执行（调用方只收集 StreamText，
无人应答 PermissionRequest），透传后 `ask` 决策 yield 的 future 永远无人
`set_result`，fork Agent 会永久挂死。传 None 和透传都是错的，需要一个
非交互适配层。

### 修法

新增 `_AskDenyingChecker`（继承 `PermissionChecker`）：规则引擎、危险命令、
路径沙箱、模式判定全部沿用父 checker（无父 checker 时按 work_dir 构造默认值），
仅把 `ask` 决策压成 `deny`（"non-interactive agent cannot prompt user"）——
与 `agent/noninteractive_tools.py::_check_noninteractive_permission` 的
既有非交互语义保持一致。`BYPASS`/`DONT_ASK` 模式下的 fork 行为不变
（用户显式选择了这些模式，`mode_decide` 直接放行）。

副作用说明：fork 中被拒绝的工具以 error 结果返回给 fork Agent 的 LLM，
fork 可能会改用允许的工具完成任务——这是权限门应有的语义。

### 回归测试

`tests/unit/skills/test_executor.py::test_fork_cannot_execute_ask_tools_without_approval`
（fork 中对 `touch pwned.txt` 的调用——非安全命令、DEFAULT 模式下为 ask——修复前被执行，修复后被拒绝）

## ADR-003：Hook 命令注入（P0-3）

### 根因

`HookContext.expand()`（hooks/models.py）把 `$FILE_PATH`/`$TOOL_ARGS.*`
原文替换进模板，`execute_command`（hooks/executors.py）把展开后的字符串
直接交给 `asyncio.create_subprocess_shell`。工具参数是**模型输出**：
文件名含 `; rm -rf ~`（POSIX）或 `& calc`（Windows）即注入执行。

### 修法

`expand()` 增加 `shell_quote=True` 参数，变量值经 `_shell_quote_value()`
引用后再替换；`execute_command` 是唯一以 `shell_quote=True` 调用的执行器。
http url/body、prompt 文本等非 shell 场景保持默认不引用（quote 会破坏
URL 和文本语义）。

`_shell_quote_value` 按平台分派：

- **POSIX**：`shlex.quote`（单引号包裹）。
- **Windows**：`^` 转义 cmd 元字符（`& | < > ^ " !`）。审查报告原定
  "必须 shlex.quote"，但实测 Windows 的 `create_subprocess_shell` 走 cmd.exe，
  **cmd 不识别单引号**，shlex.quote 形同虚设（回归测试抓到了这一点：
  `echo 'a & echo INJECTED'` 在 cmd 下照样执行了注入命令）。故 Windows
  分支改用 `^` 转义。

已知残留（有意不做，记录在案）：cmd 的 `%VAR%` 展开无法用 `^` 可靠转义，
注入面残余极窄（需要环境变量恰有攻击者可控语义），彻底解法是改用
`create_subprocess_exec` + 显式参数表——但那会破坏 hook 模板的 shell 语法
（管道、重定向），属于 hook 引擎的接口级重设计，留给后续阶段评估。

### 回归测试

`tests/unit/hooks/test_executors.py`
- `test_expand_shell_quote_wraps_values`（引用语义 + 默认不引用）
- `test_command_action_neutralizes_shell_injection`（`a & echo INJECTED` 注入实测：修复前注入命令真实执行，修复后仅作为字面量输出）

## 修复对照表

| 审计项 | 修复位置 | 回归测试 |
|---|---|---|
| P0-1 沙箱穿越 | permissions/sandbox.py（fallback 后 resolve 重查） | test_permissions.py::TestPathSandbox 三个新用例 |
| P0-2 fork 零权限 | skills/executor.py（_AskDenyingChecker + 默认 checker） | test_skills/test_executor.py::test_fork_cannot_execute_ask_tools_without_approval |
| P0-3 Hook 注入 | hooks/models.py + hooks/executors.py（shell_quote） | test_executors.py 两个新用例 |
