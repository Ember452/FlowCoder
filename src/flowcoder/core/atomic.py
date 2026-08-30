"""原子文件写原语（P5c/P5 系列提取的公共模式）。

三处使用者（scheduler/store.py、watchdog/store.py、daemon/outbox.py）
共享同一模式：NamedTemporaryFile 落同目录临时文件 → rename 替换目标 →
失败时清理临时文件。rename 在同一文件系统上原子，避免写一半崩溃留下
半截 JSON/JSONL 被读到。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def write_text_atomic(path: Path | str, text: str) -> None:
    """原子写文本文件（临时文件 + 同目录 rename）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with fd:
            fd.write(text)
        Path(fd.name).replace(path)
    except OSError:
        Path(fd.name).unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path | str, payload: object) -> None:
    """原子写 JSON 文件（ensure_ascii=False、缩进 2）。"""
    path = Path(path)
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))
