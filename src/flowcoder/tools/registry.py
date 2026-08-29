"""工具注册表 ToolRegistry。

登记、启用/禁用、schema 与 deferred 搜索。"""

from __future__ import annotations

from typing import Any

from flowcoder.tools.base import Tool


class ToolRegistryError(ValueError):
    pass


def schema_for_protocol(tool: Tool, protocol: str) -> dict[str, Any]:
    # 不同协议的 tools schema 形态不同：Anthropic 用 input_schema 顶层平铺；
    # OpenAI 系列要求包成 {type:function, name, description, parameters}。
    base = tool.get_schema()
    if protocol in ("openai", "openai-compat"):
        return {
            "type": "function",
            "name": base["name"],
            "description": base["description"],
            "parameters": base["input_schema"],
        }
    return base


class ToolRegistry:
    """工具注册表：登记所有可用工具，管理启用/禁用与 deferred 懒加载。

    三类状态：
    - ``_tools``：已注册的工具（name → Tool）
    - ``_disabled``：被显式禁用的工具名（schema 不下发给 LLM）
    - ``_discovered``：deferred 工具中被 ToolSearch "发现"过的名字
      ——deferred 工具默认不暴露给 LLM，只有被发现后才纳入 schema 列表，
      避免 100+ 工具一次性塞满 context window。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolRegistryError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()

    def mark_discovered(self, name: str) -> None:
        # ToolSearch 命中后调用：把 deferred 工具标记为已发现，之后才会暴露给 LLM
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    @staticmethod
    def _is_deferred(tool: Tool) -> bool:
        # should_defer=True 的工具默认不进 schema，需经 ToolSearch 发现后才启用
        return bool(getattr(tool, "should_defer", False))

    def get_deferred_tool_names(self) -> list[str]:
        # 待发现（deferred 且未被禁用且尚未发现）的工具名，用于提醒 LLM 可搜索
        return [
            name
            for name, tool in self._tools.items()
            if self._is_deferred(tool)
            and name not in self._discovered
            and name not in self._disabled
        ]

    def _is_deferred_searchable(self, name: str, tool: Tool) -> bool:
        return self._is_deferred(tool) and name not in self._disabled

    def search_deferred(
        self,
        query: str,
        max_results: int,
        protocol: str = "anthropic",
    ) -> list[dict[str, Any]]:
        # 轻量打分式搜索：用名称/描述的子串与分词命中数排序，无需向量检索。
        # 评分权重：名称整串命中 +10 > 描述整串命中 +5 > 名称分词命中 +3 > 描述分词命中 +1
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not self._is_deferred_searchable(name, tool):
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        # 按分数降序取前 max_results，返回对应协议的 schema
        scored.sort(key=lambda x: x[0], reverse=True)
        return [schema_for_protocol(tool, protocol) for _, _name, tool in scored[:max_results]]

    def find_deferred_by_names(
        self,
        names: list[str],
        protocol: str = "anthropic",
    ) -> list[dict[str, Any]]:
        # 按精确名字批量加载 deferred 工具的 schema（LLM 用 ToolSearch 指名加载时走这里）
        results: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not self._is_deferred_searchable(name, tool):
                continue
            results.append(schema_for_protocol(tool, protocol))
        return results

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        # 下发给 LLM 的工具 schema：跳过被禁用的，也跳过 deferred 但尚未被发现的
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if self._is_deferred(tool) and name not in self._discovered:
                continue
            schemas.append(schema_for_protocol(tool, protocol))
        return schemas
