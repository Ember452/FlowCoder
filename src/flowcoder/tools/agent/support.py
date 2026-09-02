"""子 Agent 权限、注册表与命名辅助。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from flowcoder.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)


PERMISSION_MODE_MAP = {
    "default": PermissionMode.DEFAULT,
    "acceptEdits": PermissionMode.ACCEPT_EDITS,
    "dontAsk": PermissionMode.DONT_ASK,
}


def resolve_permission_mode(value: str | None) -> PermissionMode:
    """把字符串权限模式（如 "dontAsk"）解析为枚举；未知或 None 归为 DEFAULT。"""
    if value is None:
        return PermissionMode.DEFAULT
    return PERMISSION_MODE_MAP.get(value, PermissionMode.DEFAULT)


def create_subagent_permission_checker(
    work_dir: str,
    permission_mode: str | PermissionMode | None,
) -> PermissionChecker:
    """构建子 Agent 的权限检查器（危险命令检测 + 工作目录沙箱 + 规则引擎）。"""
    mode = (
        permission_mode
        if isinstance(permission_mode, PermissionMode)
        else resolve_permission_mode(permission_mode)
    )
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(),
        mode=mode,
    )


def unique_agent_name(base_name: str, existing_names: Iterable[str]) -> str:
    """为队友生成不与已有成员冲突的名字：优先 base_name，冲突则追加 -2/-3… 后缀。"""
    existing = set(existing_names)
    if base_name not in existing:
        return base_name
    counter = 2
    while f"{base_name}-{counter}" in existing:
        counter += 1
    return f"{base_name}-{counter}"


def parent_has_full_registry(parent_agent: Any) -> bool:
    return getattr(parent_agent, "_full_registry", None) is not None


def resolve_parent_registry(parent_agent: Any) -> Any:
    return getattr(parent_agent, "_full_registry", None) or parent_agent.registry


def resolve_parent_trace_id(parent_agent: Any) -> str:
    return parent_agent.trace_id or parent_agent.agent_id
