"""从工具参数抽取权限匹配内容与沙箱路径。"""

from __future__ import annotations

from typing import Any

_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}

_SANDBOX_PATH_FIELDS: dict[str, tuple[str, str]] = {
    "ReadFile": ("file_path", ""),
    "WriteFile": ("file_path", ""),
    "EditFile": ("file_path", ""),
    "Glob": ("path", "."),
    "Grep": ("path", "."),
}


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    """抽取工具参数中用于权限匹配的文本内容；未知工具或缺省参数时返回空串。"""
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    value = arguments.get(field, "")
    return "" if value is None else str(value)


def extract_sandbox_path(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """抽取用于沙箱校验的路径参数；未知工具返回 None，参数缺失时回落到预设默认值。"""
    field_spec = _SANDBOX_PATH_FIELDS.get(tool_name)
    if field_spec is None:
        return None
    field, default = field_spec
    value = arguments.get(field, default)
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return value
