"""门控状态持久化（P5b）：重启后不重发已发过的提醒。

单 JSON 文件 + 原子写（与 scheduler/store.py 同构）：
- delivered_keys：已送达的 delivery_key 全集（永不重发的依据）
- delivery_times / daily_counts / last_delivery_at：冷却与衰减的输入
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flowcoder.core.atomic import write_json_atomic
from flowcoder.watchdog.gate import GateConfig, GateState

logger = logging.getLogger(__name__)

DEFAULT_KEY_LIMIT = 500


class GateStateStore:
    def __init__(self, path: Path | str, *, key_limit: int = DEFAULT_KEY_LIMIT) -> None:
        self._path = Path(path)
        self._key_limit = key_limit

    def load(self) -> GateState:
        """从 JSON 恢复门控状态；缺失/损坏时返回空状态（重启不重复打扰的兜底）。"""
        if not self._path.exists():
            return GateState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("看门狗状态文件损坏，忽略并从空状态开始：%s", e)
            return GateState()
        state = GateState(
            delivered_keys=set(raw.get("delivered_keys", [])),
            delivery_times=list(raw.get("delivery_times", [])),
            daily_counts=dict(raw.get("daily_counts", {})),
            last_delivery_at=raw.get("last_delivery_at"),
        )
        state.delivered_keys = set(list(state.delivered_keys)[-self._key_limit :])
        return state

    def save(self, state: GateState, *, config: GateConfig | None = None) -> None:
        """把门控状态原子写回磁盘，delivery_times 按门控衰减历史上限裁剪。"""
        limit = config.history_limit if config else 200
        payload = {
            "delivered_keys": sorted(state.delivered_keys)[-self._key_limit :],
            "delivery_times": state.delivery_times[-limit:],
            "daily_counts": state.daily_counts,
            "last_delivery_at": state.last_delivery_at,
        }
        write_json_atomic(self._path, payload)
