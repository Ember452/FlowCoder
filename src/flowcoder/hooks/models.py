"""Hook 数据模型定义。

定义了 Hook 系统的核心数据结构：
- Action: Hook 动作配置（command / prompt / http / agent 四种类型）
- Hook: 完整的 Hook 配置项（事件 + 条件 + 动作 + reject/once/async 标记）
- HookContext: Hook 执行时的上下文（事件名、工具名、参数、文件路径等），
  支持模板变量展开（$EVENT / $TOOL_NAME / $FILE_PATH / $TOOL_ARGS.xxx）
- ToolRejectedError: pre_tool_use Hook 拒绝工具执行时抛出的异常
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from flowcoder.hooks.conditions import ConditionGroup


_EXPANSION_RE = re.compile(
    r"\$(?P<name>EVENT|TOOL_NAME|FILE_PATH|MESSAGE|ERROR)"
    r"|\$TOOL_ARGS\.(?P<arg>[A-Za-z_][A-Za-z0-9_]*)"
)

# cmd.exe 的元字符集（不含 %：cmd 无法用 ^ 可靠转义百分号展开，见 ADR）
_CMD_METACHARS_RE = re.compile(r'([&|<>^"!])')


def _shell_quote_value(value: str) -> str:
    """按平台把变量值引用为单个 shell 字面量。

    POSIX 用 shlex.quote；Windows 的 create_subprocess_shell 走 cmd.exe，
    它不识别单引号，shlex.quote 形同虚设，须用 ^ 转义元字符。
    """
    if os.name == "nt":
        return _CMD_METACHARS_RE.sub(r"^\1", value)
    return shlex.quote(value)


@dataclass
class Action:
    """Hook 的动作配置。支持 4 种类型：
    - command: 执行 shell 命令（如 lint / 格式化）
    - prompt: 注入文本给 LLM（如提醒 / 修改行为）
    - http: 发 HTTP 请求（如通知外部系统 / Webhook）
    - agent: 派生子 Agent 处理（如自动代码审查）
    """

    type: str
    command: str = ""  # command 类型的 shell 命令
    message: str = ""  # prompt 类型的注入消息
    url: str = ""  # http 类型的请求 URL
    method: str = "POST"  # http 类型的请求方法
    body: str = ""  # http 类型的请求体
    headers: dict[str, str] = field(default_factory=dict)  # http 请求头
    prompt: str = ""  # agent 类型的子 Agent 提示
    timeout: int = 30  # 超时时间（秒）


@dataclass
class ActionResult:
    output: str = ""
    success: bool = True


@dataclass
class Hook:
    """一个 Hook 配置项：在指定事件触发时，如果条件匹配，执行对应动作。

    配置示例（config.yaml）：
        hooks:
          - id: "lint-on-save"
            event: "post_tool_use"
            condition: { tool_name: "WriteFile" }
            action: { type: "command", command: "npx eslint $FILE_PATH" }
            reject: false       # pre_tool_use 时可设为 true 拦截工具
            once: false         # 设为 true 则只执行一次
            async_exec: false   # 设为 true 则后台异步执行
    """

    id: str
    event: str  # 触发事件名（对应 LifecycleEvent）
    action: Action  # 要执行的动作
    condition: ConditionGroup | None = None  # 触发条件（可为 None = 无条件）
    reject: bool = False  # pre_tool_use 时是否拒绝工具执行
    once: bool = False  # 是否只执行一次
    async_exec: bool = False  # 是否后台异步执行（不阻塞 Agent 循环）
    executed: bool = False  # 是否已执行过（配合 once 使用）

    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True

    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookContext:
    """Hook 执行时的上下文信息，包含当前事件和工具调用的相关数据。

    支持模板变量展开（$EVENT / $TOOL_NAME / $FILE_PATH / $MESSAGE / $ERROR / $TOOL_ARGS.xxx），
    用于在 command / http / prompt 等动作中引用上下文数据。
    """

    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name.startswith("args."):
            key = name[5:]
            if key not in self.tool_args:
                return ""
            value = self.tool_args[key]
            return "" if value is None else str(value)
        return ""

    def expand(self, template: str, shell_quote: bool = False) -> str:
        """展开模板变量。

        shell_quote=True 时把变量值按平台引用为 shell 字面量，用于将要经过
        create_subprocess_shell 的 command 场景——文件名/参数里的
        ``;`` ``&`` ``|`` 等元字符会被当作字面量而非命令分隔符；
        http url/body、prompt 文本等非 shell 场景必须保持 False。
        """

        def replace(match: re.Match[str]) -> str:
            arg_key = match.group("arg")
            if arg_key is not None:
                if arg_key not in self.tool_args:
                    return match.group(0)
                value = self.tool_args[arg_key]
                value = "" if value is None else str(value)
                return _shell_quote_value(value) if shell_quote else value

            name = match.group("name")
            if name == "EVENT":
                value = self.event_name
            elif name == "TOOL_NAME":
                value = self.tool_name
            elif name == "FILE_PATH":
                value = self.file_path
            elif name == "MESSAGE":
                value = self.message
            elif name == "ERROR":
                value = self.error
            else:
                return match.group(0)
            return _shell_quote_value(value) if shell_quote else value

        return _EXPANSION_RE.sub(replace, template)


class ToolRejectedError(Exception):
    """pre_tool_use Hook 拒绝工具执行时抛出的异常。

    当 Hook 配置了 reject: true 且条件匹配时，Agent 会跳过工具执行，
    将拒绝原因作为工具结果返回给 LLM。
    """

    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
