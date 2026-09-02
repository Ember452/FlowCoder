"""判定环节：信号触发 Agent 判断"是否值得主动提示"（P5b）。

prompt 内置标准（宁可少打扰）：只有当信号意味着**用户需要知道、且不及时
说就会造成损失、且没有更合适的被动查看时机**时才值得提示；纯状态同步、
用户自己一眼能看到的信息一律不值得。

结构化输出：模型以 ```json {"worth_prompting": bool, "reason": str}``` 回答。
解析失败按"不值得"处理（保守降级——看门狗的失败模式应当是沉默而非骚扰）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from flowcoder.watchdog.signals import Signal

logger = logging.getLogger(__name__)

JUDGE_PROMPT_TEMPLATE = """你是编码助手 FlowCoder 的看门狗。仓库发生了一个事件，请判断是否值得**主动打断用户**提示它。

判定标准（全部满足才值得提示）：
1. 用户需要知道：事件影响用户关心的工作成果或即将进行的工作；
2. 不及时说有损失：存在用户不知情时会恶化或造成返工的风险；
3. 无法被动获知：用户下次自己查看时会错过或为时已晚；
4. 一次说清：不需要追问就能理解，提示本身自带结论或明确下一步。

反例（不值得）：纯状态同步（工作区有未提交变更）、用户自己一打开就能看到的信息、与当前工作无关的琐碎变动。

事件类型：{kind}
事件摘要：{summary}

请只输出一个 JSON（用 ```json 代码块包裹）：
{{"worth_prompting": true/false, "reason": "一句话理由"}}"""


@dataclass(frozen=True)
class Verdict:
    worth_prompting: bool
    reason: str


class WorthinessJudge(Protocol):
    """判定器抽象：LLMJudge（真实）或测试注入 fake。"""

    async def judge(self, signal: Signal) -> Verdict: ...


_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_verdict(text: str) -> Verdict:
    """从模型输出解析判定；解析失败保守返回"不值得"。"""
    match = _JSON_RE.search(text)
    if match is None:
        # 兜底：全文直接是 JSON
        try:
            payload = json.loads(text.strip())
        except json.JSONDecodeError:
            return Verdict(False, "输出无法解析，保守不提示")
    else:
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return Verdict(False, "输出无法解析，保守不提示")
    worth = payload.get("worth_prompting")
    reason = str(payload.get("reason", ""))[:200]
    if not isinstance(worth, bool):
        return Verdict(False, "worth_prompting 非布尔，保守不提示")
    return Verdict(worth_prompting=worth, reason=reason)


class LLMJudge:
    """驱动一次轻量 LLM 调用做判定（同步请求，非流式消费）。"""

    def __init__(self, client) -> None:  # client: LLMClient（含韧性层）
        self._client = client

    async def judge(self, signal: Signal) -> Verdict:
        """驱动一次 LLM 调用判定价值；LLM 不可用时保守判定"不值得"。"""
        from flowcoder.client.errors import LLMError
        from flowcoder.conversation import ConversationManager

        conversation = ConversationManager()
        conversation.add_user_message(
            JUDGE_PROMPT_TEMPLATE.format(kind=signal.kind, summary=signal.summary)
        )
        try:
            text_parts: list[str] = []
            async for event in self._client.stream(conversation):
                if hasattr(event, "text") and isinstance(event.text, str):
                    text_parts.append(event.text)
                if type(event).__name__ == "StreamEnd":
                    break
            return parse_verdict("".join(text_parts))
        except LLMError as e:
            # LLM 不可用时沉默：看门狗的失败模式是漏报而非骚扰
            logger.warning("看门狗判定调用失败，保守跳过：%s", e)
            return Verdict(False, f"判定调用失败：{e}")
