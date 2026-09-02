"""OpenAI 流式载荷 → 内部 StreamEvent / 用量。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from flowcoder.tools.base import (
    StreamEnd,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)

RESPONSE_REASONING_DELTA_EVENTS = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning.delta",
}

# 推理"完成"事件集合：不同 API 版本命名不同，统一归一处理
RESPONSE_REASONING_DONE_EVENTS = {
    "response.reasoning_summary_text.done",
    "response.reasoning_text.done",
    "response.reasoning.done",
}


def as_dict(value: Any) -> dict[str, Any]:
    """把 Pydantic v2 模型 / dataclass / dict 统一归一为普通 dict；其余类型兜底为空 dict。"""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            data = value.model_dump(exclude_none=True)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def get_text(value: Any, *names: str) -> str:
    """按优先级从属性或 dict 字段中取首个非空字符串；推理内容各 provider 字段名不一致，故多键兜底。"""
    for name in names:
        item = getattr(value, name, None)
        if isinstance(item, str) and item:
            return item
    data = as_dict(value)
    for name in names:
        item = data.get(name)
        if isinstance(item, str) and item:
            return item
    return ""


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """把流式累加的参数字符串解析成 dict；空串或非法 JSON 兜底为空 dict。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class OpenAIResponseToolCallState:
    """解析 OpenAI Responses API 的 function_call 流式增量。

    Responses API 的工具调用分三段到达：
    1. ``response.output_item.added`` → 给出工具身份（name + call_id），产出 ToolCallStart
    2. ``response.function_call_arguments.delta`` → 逐片拼接参数 JSON，产出 ToolCallDelta
    3. ``response.function_call_arguments.done`` → 参数流结束，解析累计的 JSON 产出 ToolCallComplete

    单次只跟踪一个工具调用（Responses API 串行下发），所以用单组字段而非字典。
    """

    tool_name: str = ""
    call_id: str = ""
    arguments_json: str = ""

    def _update_identity(self, source: Any) -> None:
        # 兜底：某些 provider 不发 output_item.added，身份需从后续事件里补全
        if not self.tool_name:
            self.tool_name = getattr(source, "name", "") or ""
        if not self.call_id:
            self.call_id = getattr(source, "call_id", "") or ""

    def add_output_item(self, item: Any) -> ToolCallStart | None:
        # 阶段 1：output_item.added 携带工具身份，记录并发出 ToolCallStart
        if not item or getattr(item, "type", "") != "function_call":
            return None
        self.tool_name = getattr(item, "name", "") or ""
        self.call_id = getattr(item, "call_id", "") or ""
        self.arguments_json = ""
        return ToolCallStart(tool_name=self.tool_name, tool_id=self.call_id)

    def add_arguments_delta(
        self,
        event: Any,
    ) -> list[ToolCallStart | ToolCallDelta]:
        # 阶段 2：参数 JSON 分片到达，累加到 arguments_json 并转发 delta
        events: list[ToolCallStart | ToolCallDelta] = []
        if not self.tool_name:
            # 缺身份时先从当前事件补全，再补发一次 ToolCallStart
            self._update_identity(event)
            if self.tool_name:
                events.append(ToolCallStart(tool_name=self.tool_name, tool_id=self.call_id))
        delta = getattr(event, "delta", "") or ""
        self.arguments_json += delta
        events.append(ToolCallDelta(text=delta))
        return events

    def complete(self, event: Any) -> ToolCallComplete:
        # 阶段 3：参数流结束，把累计的 JSON 解析为 dict，产出 ToolCallComplete 并清空状态
        if not self.tool_name:
            self._update_identity(event)
        complete = ToolCallComplete(
            tool_id=self.call_id,
            tool_name=self.tool_name,
            arguments=parse_tool_arguments(self.arguments_json),
        )
        self.tool_name = ""
        self.call_id = ""
        self.arguments_json = ""
        return complete


@dataclass
class OpenAIChatToolCallState:
    """解析 Chat Completions API 的流式工具调用。

    与 Responses API 不同，Chat Completions 可在一轮里并发下发多个工具调用，
    每个 delta 用 ``index`` 标识它属于哪个调用。因此用 ``index → {id,name,args}``
    字典同时跟踪多个正在拼装的调用，最后按 index 排序一次性收尾。
    """

    active_calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def add_tool_call_deltas(
        self,
        tool_calls: Any,
    ) -> list[ToolCallStart | ToolCallDelta]:
        events: list[ToolCallStart | ToolCallDelta] = []
        for tool_call in tool_calls or []:
            # index 是关联同一工具调用各分片的键（并发场景下区分多个调用）
            index = getattr(tool_call, "index", 0)
            if index not in self.active_calls:
                self.active_calls[index] = {"id": "", "name": "", "args": ""}
            call = self.active_calls[index]

            # id 通常只在首片出现，记录后续收尾时回填
            call_id = getattr(tool_call, "id", "") or ""
            if call_id:
                call["id"] = call_id
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", "") if function else ""
            if name:
                # name 同样多在首片出现，首次拿到时发出 ToolCallStart
                call["name"] = name
                events.append(ToolCallStart(tool_name=call["name"], tool_id=call["id"]))
            arguments = getattr(function, "arguments", "") if function else ""
            if arguments:
                # 参数 JSON 分片累加，每片都转发给前端实时展示
                call["args"] += arguments
                events.append(ToolCallDelta(text=arguments))
        return events

    def complete(self) -> list[ToolCallComplete]:
        # finish_reason=tool_calls 时调用：按 index 排序逐个解析 JSON 收尾
        completed = [
            ToolCallComplete(
                tool_id=call["id"],
                tool_name=call["name"],
                arguments=parse_tool_arguments(call["args"]),
            )
            for _index, call in sorted(self.active_calls.items())
        ]
        self.active_calls.clear()
        return completed


@dataclass
class OpenAIReasoningState:
    """聚合 reasoning / thinking 流式增量。

    不同 provider 暴露推理内容的时机不一致：有的逐 delta 流式给出，
    有的只在结束时给一份完整 summary。本状态机兼容三种完成来源：
    ``complete_from_done_text``（done 事件带最终文本）、
    ``complete_from_summary``（response.completed 带汇总）、
    ``complete_if_pending``（finish_reason 收尾时兜底）。
    ``completed`` 标记防止重复产出 ThinkingComplete。
    """

    text: str = ""
    completed: bool = False

    def add_delta(self, text: str) -> list[ThinkingDelta]:
        # 流式推理增量：累加到 text 并实时转发给前端
        if not text:
            return []
        self.text += text
        return [ThinkingDelta(text=text)]

    def complete_from_done_text(
        self,
        text: str,
    ) -> list[ThinkingDelta | ThinkingComplete]:
        # done 事件给出推理最终文本：补齐未收到的尾巴再收尾
        events: list[ThinkingDelta | ThinkingComplete] = []
        if text and not self.text.endswith(text):
            self.text += text
            events.append(ThinkingDelta(text=text))
        complete = self.complete_if_pending()
        if complete is not None:
            events.append(complete)
        return events

    def complete_from_summary(
        self,
        summary: str,
    ) -> list[ThinkingDelta | ThinkingComplete]:
        # response.completed 只给汇总、没给 delta 时：整段作为推理内容一次性产出
        if self.completed or not summary:
            return []
        self.text = summary
        self.completed = True
        return [
            ThinkingDelta(text=summary),
            ThinkingComplete(thinking=summary, signature=""),
        ]

    def complete_if_pending(self) -> ThinkingComplete | None:
        # 兜底收尾：已有累计文本但还没产出 ThinkingComplete 时由 finish_reason 触发
        if self.completed or not self.text:
            return None
        self.completed = True
        return ThinkingComplete(thinking=self.text, signature="")


def extract_response_reasoning_summary(response: Any) -> str:
    # Responses API 完成时从 output 里挑出 type=reasoning 的 item，拼接其 summary 文本
    data = as_dict(response)
    output = data.get("output")
    if not isinstance(output, list):
        output = getattr(response, "output", [])
    parts: list[str] = []
    for item in output or []:
        item_data = as_dict(item)
        if item_data.get("type") != "reasoning":
            continue
        summary = item_data.get("summary") or getattr(item, "summary", [])
        for summary_item in summary or []:
            text = get_text(summary_item, "text")
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def extract_chat_reasoning_delta(delta: Any) -> str:
    # Chat Completions 各家 provider 把推理内容放在不同字段，这里按优先级逐一尝试
    text = get_text(
        delta,
        "reasoning_content",
        "reasoning_delta",
        "reasoning",
        "thinking",
    )
    if text:
        return text
    data = as_dict(delta)
    reasoning = data.get("reasoning")
    if isinstance(reasoning, dict):
        for key in ("content", "text", "summary"):
            item = reasoning.get(key)
            if isinstance(item, str) and item:
                return item
    return ""


def token_count(value: Any) -> int:
    # bool 是 int 子类，需单独排除以免把 True/False 计成 token
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    return 0


def cached_tokens(details: Any) -> int:
    if details is None:
        return 0
    return token_count(getattr(details, "cached_tokens", 0))


def openai_usage_stream_end(
    *,
    total_input_tokens: Any,
    output_tokens: Any,
    cache_details: Any,
) -> StreamEnd:
    # OpenAI 把命中缓存的 token 也算进 total_input_tokens。StreamEnd 报告的是
    # 未命中缓存的 input + cache_read，二者相加才能还原真实 prompt 大小
    # （与 tools/base.py 中 StreamEnd 的 cache 字段语义一致）。
    cache_read = cached_tokens(cache_details)
    input_tokens = token_count(total_input_tokens)
    return StreamEnd(
        stop_reason="end_turn",
        input_tokens=max(input_tokens - cache_read, 0),
        output_tokens=token_count(output_tokens),
        cache_read=cache_read,
        cache_creation=0,
    )


def stream_end_from_openai_response_usage(usage: Any) -> StreamEnd:
    # Responses API 用量字段：input_tokens / output_tokens / input_tokens_details
    return openai_usage_stream_end(
        total_input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_details=getattr(usage, "input_tokens_details", None),
    )


def stream_end_from_openai_chat_usage(usage: Any) -> StreamEnd:
    # Chat Completions 用量字段：prompt_tokens / completion_tokens / prompt_tokens_details
    return openai_usage_stream_end(
        total_input_tokens=getattr(usage, "prompt_tokens", 0),
        output_tokens=getattr(usage, "completion_tokens", 0),
        cache_details=getattr(usage, "prompt_tokens_details", None),
    )
