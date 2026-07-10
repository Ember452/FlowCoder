"""本地 A2A 传输适配包。

把 Daemon 中的 Agent 暴露为 A2A 兼容任务接口，供外部协议对接。"""

from flowcoder.a2a.bridge import A2ABridge
from flowcoder.a2a.protocol import A2AError
from flowcoder.a2a.tasks import A2ATask

__all__ = [
    "A2ABridge",
    "A2AError",
    "A2ATask",
]
