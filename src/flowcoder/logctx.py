"""日志追踪上下文。

trace_id 通过 contextvar 在同一 Agent 调用链内传递（含子 Agent 与其内部
任务），由 TraceIdLogFilter 注入到每条日志记录；Agent 循环外层统一在此
模块配置日志（轮转文件 + trace_id 格式），避免各入口各写一份 basicConfig。

依赖方向：本模块是最底层基础设施，不 import 任何 flowcoder 上层模块。
"""

from __future__ import annotations

import logging
import logging.handlers
import uuid
from collections.abc import Iterator
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar, Token

TRACE_ID_NONE = "-"

# 带字段的日志格式：trace_id 由 TraceIdLogFilter 保证存在
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s [trace_id=%(trace_id)s] %(message)s"

_trace_id_var: ContextVar[str | None] = ContextVar("flowcoder_trace_id", default=None)

# 长跑 daemon 的日志文件必须有上限；单文件 10MB、保留 3 个历史
_MAX_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def bind_trace_id(trace_id: str) -> Token[str | None]:
    """绑定 trace_id，返回 token 供 finally 中 reset（asyncio 任务间自动隔离）。"""
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _trace_id_var.reset(token)


@contextmanager
def trace_id_context(trace_id: str) -> Iterator[str]:
    """在代码块内绑定 trace_id，退出自动恢复；嵌套绑定遵循栈语义。"""
    token = bind_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        reset_trace_id(token)


class TraceIdLogFilter(logging.Filter):
    """把 contextvar 里的 trace_id 注入 LogRecord；未绑定时填占位符。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id_var.get() or TRACE_ID_NONE
        return True


def setup_logging(
    *,
    level: int = logging.INFO,
    filename: str | None = None,
    filemode: str = "w",
) -> None:
    """进程级日志配置：统一格式（含 trace_id）+ 文件轮转。

    filename 为 None 时输出到 stderr（daemon 场景）；给定时写轮转文件
    （CLI / scheduler 场景）。重复调用以最后一次为准。
    """
    handler: logging.Handler
    if filename is not None:
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            filename,
            mode=filemode,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(TraceIdLogFilter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
