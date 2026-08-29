"""sandbox 单元测试的 fake 运行时：记录全部调用，按注入的 exec 行为返回。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from flowcoder.sandbox.runtime import ExecOutcome

ExecFn = Callable[[str, list[str], str], ExecOutcome]


@pytest.fixture
def fake_runtime() -> FakeRuntime:
    return FakeRuntime()


class FakeRuntime:
    """实现 ContainerRuntime 协议的 fake：不碰 Docker，只记录。"""

    def __init__(self) -> None:
        self.create_specs: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.archives: list[tuple[str, bytes]] = []
        self.exec_calls: list[tuple[str, list[str], str]] = []
        self.kill_signals: list[str] = []
        self.removed: list[tuple[str, bool]] = []
        #: 注入的自定义 exec 行为；None 时返回退出码 0 的空结果
        self.exec_fn: ExecFn | None = None

    def create(self, spec: Any) -> str:
        self.create_specs.append(dict(spec))
        return "cid-0001"

    def start(self, container_id: str) -> None:
        self.started.append(container_id)

    def put_archive(self, container_id: str, path: str, data: bytes) -> None:
        self.archives.append((path, data))

    def exec_run(self, container_id: str, cmd: list[str], workdir: str) -> ExecOutcome:
        self.exec_calls.append((container_id, cmd, workdir))
        if self.exec_fn is not None:
            return self.exec_fn(container_id, cmd, workdir)
        return ExecOutcome(0, "", "")

    def kill(self, container_id: str, signal: str) -> None:
        self.kill_signals.append(signal)

    def remove(self, container_id: str, *, force: bool) -> None:
        self.removed.append((container_id, force))


def blocking_exec(seconds: float) -> ExecFn:
    """模拟一次长时间挂死的 exec（真实场景：进程 hang、daemon 无响应）。"""

    def _fn(_cid: str, _cmd: list[str], _workdir: str) -> ExecOutcome:
        time.sleep(seconds)
        return ExecOutcome(0, "", "")

    return _fn
