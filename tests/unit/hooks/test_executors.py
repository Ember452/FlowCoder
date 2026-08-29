"""Hook command 动作执行器的注入防护测试。"""

from __future__ import annotations

from flowcoder.hooks.executors import execute_command
from flowcoder.hooks.models import Action, HookContext, _shell_quote_value


def test_expand_shell_quote_wraps_values() -> None:
    ctx = HookContext(file_path="a; rm -rf ~", tool_args={"cmd": "x & y"})
    assert ctx.expand("echo $FILE_PATH", shell_quote=True) == (
        f"echo {_shell_quote_value('a; rm -rf ~')}"
    )
    assert ctx.expand("run $TOOL_ARGS.cmd", shell_quote=True) == (
        f"run {_shell_quote_value('x & y')}"
    )
    # 默认不引号（http url / prompt 文本场景不能被 quote 破坏）
    assert ctx.expand("echo $FILE_PATH") == "echo a; rm -rf ~"


async def test_command_action_neutralizes_shell_injection() -> None:
    """文件名含 shell 元字符时，注入的第二条命令不得执行。"""
    malicious_path = "a & echo INJECTED"
    ctx = HookContext(file_path=malicious_path)
    action = Action(type="command", command="echo $FILE_PATH", timeout=15)

    result = await execute_command(action, ctx)

    tokens = {line.strip().strip("'\"") for line in result.output.splitlines()}
    assert "INJECTED" not in tokens, f"命令注入成功执行，输出: {result.output!r}"
