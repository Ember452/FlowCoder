"""OpenAI Responses API 请求构造。"""

from __future__ import annotations

from typing import Any


def convert_tools_for_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把工具 schema 转换为 Responses API 的扁平 function 格式。

    Responses API 要求 ``{type, name, description, parameters}`` 扁平结构；
    内部 schema（``input_schema`` 键形态）透传会被 API 以 400 拒绝。
    已经是扁平格式的列表原样返回（保持对象同一性，方便调用方缓存判断）。
    与 openai_compat_request.convert_tools_for_chat_completions 对齐；
    两者的最终合并见审查报告的 providers 下沉计划（R2）。
    """
    if all(tool.get("type") == "function" and "name" in tool for tool in tools):
        return tools
    converted: list[dict[str, Any]] = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", tool.get("input_schema", {})),
            }
        )
    return converted


def build_openai_response_request_kwargs(
    *,
    model: str,
    input_messages: list[dict[str, Any]],
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
    thinking: bool = False,
) -> dict[str, Any]:
    """Build common OpenAI Responses streaming request arguments."""
    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_messages,
        "stream": True,
    }
    if system:
        kwargs["instructions"] = system
    if tools:
        kwargs["tools"] = convert_tools_for_responses(tools)
    if thinking:
        kwargs["reasoning"] = {"summary": "auto"}
    return kwargs
