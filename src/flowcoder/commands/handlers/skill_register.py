"""把已加载技能注册为斜杠命令。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from flowcoder.commands.registry import Command, CommandContext, CommandRegistry, CommandType

if TYPE_CHECKING:
    from flowcoder.skills.executor import SkillExecutor
    from flowcoder.skills.loader import SkillLoader

log = logging.getLogger(__name__)

_REGISTERED_SKILL_NAMES: set[str] = set()


def register_skill_commands(
    registry: CommandRegistry,
    loader: SkillLoader,
    executor: SkillExecutor | None = None,
) -> None:
    """把已加载技能注册为斜杠命令；先注销上次注册的旧技能命令。"""
    # 先清理上一次注册的技能命令，避免重载后命令与别名残留
    for name in list(_REGISTERED_SKILL_NAMES):
        if registry.find(name) is not None:
            registry._commands.pop(name, None)
            registry._alias_map = {k: v for k, v in registry._alias_map.items() if v != name}
        _REGISTERED_SKILL_NAMES.discard(name)

    for skill_name, skill_desc in loader.get_catalog():
        if registry.find(skill_name) is not None:
            continue

        s_name = skill_name
        s_desc = skill_desc

        # 用 make_handler(name) 闭包捕获当前 name，避免循环变量被后续轮次覆盖
        def make_handler(name: str) -> callable:

            async def handler(ctx: CommandContext) -> None:
                exe = ctx.config.get("skill_executor") if executor is None else executor
                if exe is None:
                    ctx.ui.add_system_message("Skill 执行器未初始化")
                    return

                skill_loader: SkillLoader | None = ctx.config.get("skill_loader")
                if skill_loader is None:
                    ctx.ui.add_system_message("Skill 加载器未初始化")
                    return

                skill = skill_loader.get(name)
                if skill is None:
                    ctx.ui.add_system_message(f"未找到 Skill：{name}")
                    return

                # fork：异步独立执行并把结果打印出来；inline：注入对话并触发后续用户消息
                if skill.mode == "fork":
                    ctx.ui.add_system_message(f"⏳ Running {name} skill...")

                    async def _run_fork() -> None:
                        try:
                            result = await exe.execute_fork(skill, ctx.args)
                            ctx.ui.add_system_message(f"[{name} skill result]\n{result}")
                        except Exception as e:
                            ctx.ui.add_system_message(f"Skill {name} failed: {e}")

                    asyncio.create_task(_run_fork())
                else:
                    exe.execute_inline(skill, ctx.args)
                    tools_info = ""
                    if skill.allowed_tools:
                        tools_info = f" · {len(skill.allowed_tools)} tools allowed"
                    ctx.ui.add_system_message(
                        f"skill({name})\nSuccessfully loaded skill{tools_info}"
                    )
                    trigger = ctx.args if ctx.args else f"/{name}"
                    ctx.ui.send_user_message(trigger)

            return handler

        cmd = Command(
            name=s_name,
            description=f"{s_desc} [skill]",
            usage=f"/{s_name} [args]",
            type=CommandType.PROMPT,
            handler=make_handler(s_name),
        )

        try:
            registry.register_sync(cmd)
            _REGISTERED_SKILL_NAMES.add(s_name)
        except ValueError as e:
            log.warning("Cannot register skill command '%s': %s", s_name, e)
