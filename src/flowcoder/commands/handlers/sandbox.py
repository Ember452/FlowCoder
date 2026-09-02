"""/sandbox 命令：查看或切换 Bash 工具的沙箱执行模式。"""

from __future__ import annotations

from flowcoder.commands.registry import Command, CommandContext, CommandType
from flowcoder.config import update_user_config_value
from flowcoder.tools.bash import SANDBOX_MODES

_MODE_NAMES = "、".join(SANDBOX_MODES)


async def handle_sandbox(ctx: CommandContext) -> None:
    """查看/切换 Bash 沙箱模式，切换成功后持久化到用户配置。"""
    if ctx.agent is None:
        ctx.ui.add_system_message("Agent 未初始化")
        return
    bash = ctx.agent.registry.get("Bash")
    if bash is None:
        ctx.ui.add_system_message("Bash 工具未注册")
        return

    sub = ctx.args.split(None, 1)[0] if ctx.args.split() else ""

    if sub == "":
        workdir = getattr(bash, "_host_workdir", None) or ctx.agent.work_dir
        ctx.ui.add_system_message(
            f"沙箱状态\n  当前模式: {bash.sandbox_mode}\n  白名单工作目录: {workdir}"
        )
        return

    if sub not in SANDBOX_MODES:
        ctx.ui.add_system_message(f"用法: /sandbox [off | docker]\n可选: {_MODE_NAMES}")
        return

    error = await bash.set_sandbox_mode(sub)
    if error is not None:
        # 切换失败（典型：未装 Docker SDK 或 daemon 不可达），模式保持不变
        ctx.ui.add_system_message(error)
        return

    path = update_user_config_value("sandbox_mode", sub)
    ctx.ui.refresh_status()
    ctx.ui.add_system_message(f"沙箱模式已切换为: {sub}\n（已持久化到 {path}，重启后仍生效）")


SANDBOX_COMMAND = Command(
    name="sandbox",
    description="查看/切换 Bash 沙箱执行模式（off=本机 subprocess，docker=容器隔离）",
    usage="/sandbox [off | docker]",
    type=CommandType.LOCAL,
    handler=handle_sandbox,
)
