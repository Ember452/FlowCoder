"""logctx：trace_id 日志上下文与统一日志配置。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from flowcoder.logctx import (
    TRACE_ID_NONE,
    TraceIdLogFilter,
    bind_trace_id,
    current_trace_id,
    new_trace_id,
    reset_trace_id,
    setup_logging,
    trace_id_context,
)


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _captured_trace_id(record: logging.LogRecord) -> str:
    return getattr(record, "trace_id", "<missing>")


def _capture_logger(name: str, handler: logging.Handler) -> logging.Logger:
    """挂一个隔离的测试 logger（不继承根级别，避免 pytest 日志配置干扰）。"""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False
    return log


def test_filter_未绑定时填充占位符() -> None:
    handler = _CaptureHandler()
    handler.addFilter(TraceIdLogFilter())
    log = _capture_logger("logctx-test.unbound", handler)
    try:
        log.info("hello")
    finally:
        log.removeHandler(handler)
        log.propagate = True

    assert _captured_trace_id(handler.records[0]) == TRACE_ID_NONE


def test_bind后日志记录携带trace_id() -> None:
    handler = _CaptureHandler()
    handler.addFilter(TraceIdLogFilter())
    log = _capture_logger("logctx-test.bound", handler)
    try:
        with trace_id_context("abc123"):
            log.info("hello")
    finally:
        log.removeHandler(handler)
        log.propagate = True

    assert _captured_trace_id(handler.records[0]) == "abc123"


def test_嵌套绑定退出后恢复外层() -> None:
    with trace_id_context("outer"):
        with trace_id_context("inner"):
            assert current_trace_id() == "inner"
        assert current_trace_id() == "outer"
    assert current_trace_id() is None


def test_token方式手动绑定与恢复() -> None:
    token = bind_trace_id("manual")
    try:
        assert current_trace_id() == "manual"
    finally:
        reset_trace_id(token)
    assert current_trace_id() is None


async def test_绑定不跨asyncio任务泄漏() -> None:
    seen: dict[str, str | None] = {}

    async def child(trace_id: str) -> None:
        with trace_id_context(trace_id):
            await asyncio.sleep(0)
            seen[trace_id] = current_trace_id()

    await asyncio.gather(child("t1"), child("t2"))

    assert seen == {"t1": "t1", "t2": "t2"}
    # 任务结束后调用方上下文不受影响
    assert current_trace_id() is None


def test_new_trace_id非空且唯一() -> None:
    ids = {new_trace_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 12 for i in ids)


def test_setup_logging挂载filter与轮转handler(tmp_path: pytest.TempPathFactory) -> None:
    log_file = tmp_path / "logs" / "debug.log"
    setup_logging(filename=str(log_file))
    try:
        root = logging.getLogger()
        assert root.handlers
        handler = root.handlers[0]
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes > 0
        assert any(isinstance(f, TraceIdLogFilter) for f in handler.filters)
        assert "%(trace_id)s" in handler.formatter._fmt  # type: ignore[union-attr]
        # 父目录自动创建
        assert log_file.parent.is_dir()
    finally:
        logging.getLogger().handlers.clear()
        logging.getLogger().addHandler(logging.NullHandler())
