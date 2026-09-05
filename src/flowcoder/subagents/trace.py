"""子 Agent 调用树追踪。

TraceNode / TraceManager 记录父子 Agent 调用关系。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TraceNode:
    agent_id: str
    parent_id: str | None
    trace_id: str
    agent_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    status: str = "running"


class TraceManager:
    """按 agent_id 登记子 Agent 调用节点，按 trace_id 聚合为调用树。"""

    def __init__(self) -> None:
        self._nodes: dict[str, TraceNode] = {}

    def create(
        self,
        agent_type: str,
        parent_id: str | None = None,
        trace_id: str | None = None,
    ) -> TraceNode:
        """创建一个追踪节点；未指定时生成新的 trace_id（标记本次调用的根）。"""
        agent_id = uuid.uuid4().hex[:12]
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:12]

        node = TraceNode(
            agent_id=agent_id,
            parent_id=parent_id,
            trace_id=trace_id,
            agent_type=agent_type,
        )
        self._nodes[agent_id] = node
        return node

    def update(self, agent_id: str, **kwargs: int | str) -> None:
        node = self._nodes.get(agent_id)
        if node is None:
            return
        for key, value in kwargs.items():
            if hasattr(node, key):
                setattr(node, key, value)

    def complete(self, agent_id: str, status: str = "completed") -> None:
        node = self._nodes.get(agent_id)
        if node is None:
            return
        node.end_time = time.monotonic()
        node.status = status

    def get(self, agent_id: str) -> TraceNode | None:
        return self._nodes.get(agent_id)

    def get_tree(self, trace_id: str) -> list[TraceNode]:
        """返回同一 trace_id 下的全部节点（整棵调用树）。"""
        return [n for n in self._nodes.values() if n.trace_id == trace_id]

    def remove(self, agent_id: str) -> None:
        self._nodes.pop(agent_id, None)

    def complete_all_running(self, parent_id: str) -> None:
        """强制收尾某父节点下所有仍在运行的子节点（用于中断/清理）。"""
        for node in self._nodes.values():
            if node.parent_id == parent_id and node.status == "running":
                node.status = "completed"
                node.end_time = time.monotonic()

    def get_total_tokens(self, trace_id: str) -> tuple[int, int]:
        total_in = 0
        total_out = 0
        for node in self._nodes.values():
            if node.trace_id == trace_id:
                total_in += node.input_tokens
                total_out += node.output_tokens
        return total_in, total_out
