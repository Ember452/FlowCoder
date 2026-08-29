"""Bash 工具：在 shell 中执行命令。

sandbox_mode=off（默认）：裸 subprocess，行为与 P1c 之前完全一致。
sandbox_mode=docker：命令进沙箱容器执行（P1c）——白名单工作目录读写挂载到
容器 /workspace，容器级限额 + 双层超时生效。模式切换经 /sandbox 斜杠命令，
先过 Docker 可用性预检，失败则保持原模式并返回明确错误（不静默降级）。
权限审批与危险命令检测发生在 agent 的授权层（先于本工具执行），两条通道
语义一致：权限门永远在沙箱之前。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex

from pydantic import BaseModel, Field

from flowcoder.tools.base import Tool, ToolResult

MAX_TIMEOUT = 600

# 特殊命令的退出码语义映射
# 这些命令的 exit code 1 不代表错误，只有 >= 阈值才算真正的错误
# 例如 grep 返回 1 仅表示"没有匹配行"，不是执行出错
_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2,  # exit 1 = 没有匹配到内容
    "egrep": 2,
    "fgrep": 2,
    "rg": 2,  # ripgrep，与 grep 语义一致
    "diff": 2,  # exit 1 = 文件内容有差异
    "find": 2,  # exit 1 = 部分成功（如权限不足跳过某些目录）
    "test": 2,  # exit 1 = 条件为假
    "[": 2,  # test 的别名形式
}

SANDBOX_MODES = ("off", "docker")

#: docker 模式的容器内工作目录（宿主白名单目录的挂载点）
SANDBOX_CONTAINER_WORKDIR = "/workspace"

#: bash 工具用的池规模：交互式会话 2 个预热容器足够，避免常驻占用过多内存
SANDBOX_POOL_SIZE = 2


def _extract_last_command_name(command: str) -> str | None:
    """从命令字符串中提取最后一个管道段的基础命令名。

    管道中最后一个命令决定了整体退出码，所以只看最后一段。
    例如 "cat file | grep pattern" → "grep"
    """
    # 按管道符拆分，取最后一段
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None

    # 跳过常见的环境变量赋值前缀，如 "FOO=bar command ..."
    # 也要处理 sudo/env 等包装命令
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        # shlex 解析失败时，用简单的空格分割兜底
        tokens = last_segment.split()

    for token in tokens:
        # 跳过形如 VAR=VALUE 的环境变量赋值
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # 取 basename（去掉路径前缀，如 /usr/bin/grep → grep）
        base = token.rsplit("/", maxsplit=1)[-1]
        return base

    return None


def _interpret_exit_code(command: str, exit_code: int) -> bool:
    """根据命令语义判断退出码是否代表真正的错误。

    返回 True 表示是错误，False 表示不是错误。
    """
    if exit_code == 0:
        return False

    cmd_name = _extract_last_command_name(command)
    if cmd_name and cmd_name in _COMMAND_ERROR_THRESHOLDS:
        # 只有退出码 >= 阈值时才视为错误
        return exit_code >= _COMMAND_ERROR_THRESHOLDS[cmd_name]

    # 默认行为：非零退出码即为错误
    return True


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


def _format_output(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if not parts:
        parts.append("(no output)")
    return "\n".join(parts)


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"

    def __init__(self, host_workdir: str | None = None, sandbox_mode: str = "off") -> None:
        # 初始模式直接生效不做可用性预检：配置声明的意图不应阻塞启动，
        # Docker 不可用时首次执行会得到明确报错（见 _execute_in_sandbox）
        self._sandbox_mode = sandbox_mode
        self._pool: object | None = None  # SandboxPool，docker 模式首次启用时创建
        self._host_workdir = host_workdir

    @property
    def sandbox_mode(self) -> str:
        return self._sandbox_mode

    async def set_sandbox_mode(self, mode: str) -> str | None:
        """切换执行通道。成功返回 None；失败返回错误信息，模式保持不变。"""
        if mode not in SANDBOX_MODES:
            return f"未知 sandbox_mode: {mode}（可选: off, docker）"
        if mode == self._sandbox_mode:
            return None
        if mode == "docker":
            try:
                pool = self._ensure_pool()
                await pool.start()  # 预热即预检：Docker 未装/daemon 不可达在这里暴露
            except Exception as e:
                self._pool = None
                return f"无法启用 docker 沙箱模式：{e}"
        else:
            await self._shutdown_pool()
        self._sandbox_mode = mode
        return None

    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)
        if self._sandbox_mode == "docker":
            return await self._execute_in_sandbox(params, timeout)
        return await self._execute_subprocess(params, timeout)

    # ------------------------------------------------------------------ off

    async def _execute_subprocess(self, params: Params, timeout: int) -> ToolResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                params.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        return ToolResult(
            output=_format_output(stdout.decode(errors="replace"), stderr.decode(errors="replace")),
            is_error=_interpret_exit_code(params.command, proc.returncode or 0),
        )

    # --------------------------------------------------------------- docker

    async def _execute_in_sandbox(self, params: Params, timeout: int) -> ToolResult:
        from flowcoder.sandbox import SandboxError

        try:
            pool = self._ensure_pool()
            result = await pool.execute(params.command, timeout_s=float(timeout))
        except SandboxError as e:
            return ToolResult(output=f"Error executing in sandbox: {e}", is_error=True)

        if result.timed_out:
            return ToolResult(
                output=f"Error: command timed out after {timeout}s (killed in sandbox)",
                is_error=True,
            )
        return ToolResult(
            output=_format_output(result.stdout, result.stderr),
            is_error=_interpret_exit_code(params.command, result.exit_code or 0),
        )

    def _ensure_pool(self):  # -> SandboxPool；延迟 import 避免让 sandbox 成为硬依赖
        from flowcoder.sandbox import SandboxConfig, SandboxPool

        if self._pool is None:
            host_dir = self._host_workdir or os.getcwd()
            config = SandboxConfig(mounts={host_dir: SANDBOX_CONTAINER_WORKDIR})
            self._pool = SandboxPool(size=SANDBOX_POOL_SIZE, config=config)
        return self._pool

    async def _shutdown_pool(self) -> None:
        if self._pool is None:
            return
        from flowcoder.sandbox import SandboxError

        try:
            await self._pool.close()  # type: ignore[attr-defined]
        except SandboxError:
            pass
        self._pool = None
