"""四维预算闸：token / 轮次 / 时间 / 成本（P3）。

预算在 Agent 循环外围强制执行（不是 prompt 恳求）：超限触发"总结并收敛"
而非硬杀——循环注入收敛请求并撤下工具 schema，给模型一轮收尾机会；
收敛轮仍不收敛才强制结束。

判定只读累计值，不做任何 IO；注入文案与收敛语义由 core.py 的最小接入点
消费（见 core.py run() 循环顶部的预算检查，diff 约 25 行）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flowcoder.config import AppConfig


@dataclass(frozen=True)
class Budget:
    """资源上限。全部 None 表示不设预算（默认，行为与无预算完全一致）。"""

    max_total_tokens: int | None = None  # 输入+输出 token 累计
    max_turns: int | None = None  # LLM 交互轮次
    max_seconds: float | None = None  # 挂钟时间
    max_cost_usd: float | None = None  # 成本（需配合下方单价）
    input_price_per_1m: float = 0.0  # 每百万 input token 单价（美元）
    output_price_per_1m: float = 0.0

    def __post_init__(self) -> None:
        if all(
            v is None
            for v in (self.max_total_tokens, self.max_turns, self.max_seconds, self.max_cost_usd)
        ):
            raise ValueError("Budget 至少要设置一项上限")
        for name in ("max_total_tokens", "max_turns"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} 必须为正数")
        for name in ("max_seconds", "max_cost_usd"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} 必须为正数")
        if (
            self.max_cost_usd is not None
            and self.input_price_per_1m <= 0
            and self.output_price_per_1m <= 0
        ):
            raise ValueError("设置 max_cost_usd 必须同时提供至少一个单价")

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_price_per_1m
            + output_tokens / 1_000_000 * self.output_price_per_1m
        )


class BudgetState:
    """一次 run() 的预算状态：从循环启动开始计时，只读累计值做判定。"""

    def __init__(self, budget: Budget) -> None:
        self._budget = budget
        self._started_at = time.monotonic()

    def reset(self) -> None:
        self._started_at = time.monotonic()

    def breach_reason(
        self,
        *,
        total_input_tokens: int,
        total_output_tokens: int,
        iteration: int,
    ) -> str | None:
        """返回首个超限原因描述；未超限返回 None。"""
        b = self._budget
        elapsed = time.monotonic() - self._started_at
        if b.max_turns is not None and iteration > b.max_turns:
            return f"轮次超限（{iteration} > {b.max_turns}）"
        if b.max_seconds is not None and elapsed > b.max_seconds:
            return f"时间超限（{elapsed:.0f}s > {b.max_seconds}s）"
        total_tokens = total_input_tokens + total_output_tokens
        if b.max_total_tokens is not None and total_tokens > b.max_total_tokens:
            return f"token 超限（{total_tokens} > {b.max_total_tokens}）"
        if b.max_cost_usd is not None:
            cost = b.cost_usd(total_input_tokens, total_output_tokens)
            if cost > b.max_cost_usd:
                return f"成本超限（${cost:.4f} > ${b.max_cost_usd:.4f}）"
        return None


def converge_message(reason: str) -> str:
    """注入对话的收敛请求（作为 user 消息，模型下一轮可见）。"""
    return f"[预算告警] {reason}。请立即总结当前进展并给出最终结论，不要再调用任何工具。"


def build_budget_from_config(config: AppConfig) -> Budget | None:
    """按配置构造 Budget（供 agent 工厂注入）；未配置返回 None。

    config 为 AppConfig（含 budget 段）。此函数原在 daemon/background.py，
    为消除 agent→daemon 反向依赖迁入本模块：config→budget 是纯映射，
    属 agent 层自身职责，daemon 只做消费。
    """
    if config is None or config.budget is None:
        return None
    b = config.budget
    return Budget(
        max_total_tokens=b.max_total_tokens,
        max_turns=b.max_turns,
        max_seconds=b.max_seconds,
        max_cost_usd=b.max_cost_usd,
        input_price_per_1m=b.input_price_per_1m,
        output_price_per_1m=b.output_price_per_1m,
    )
