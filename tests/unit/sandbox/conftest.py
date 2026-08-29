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
        self._counter = 0
        self.create_specs: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.archives: list[tuple[str, bytes]] = []
        self.exec_calls: list[tuple[str, list[str], str]] = []
        self.kill_signals: list[str] = []
        self.removed: list[tuple[str, bool]] = []
        self.stats_results: dict[str, dict[str, float]] = {}
        #: 容器存活状态；kill(SIGKILL) 与 set_alive 会改变它
        self.alive: dict[str, bool] = {}
        #: 注入的自定义 exec 行为；None 时返回退出码 0 的空结果
        self.exec_fn: ExecFn | None = None

    def create(self, spec: Any) -> str:
        self.create_specs.append(dict(spec))
        self._counter += 1
        cid = f"cid-{self._counter:04d}"
        self.alive[cid] = True
        return cid

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
        if signal == "SIGKILL":
            self.alive[container_id] = False

    def remove(self, container_id: str, *, force: bool) -> None:
        self.removed.append((container_id, force))
        self.alive[container_id] = False

    def is_alive(self, container_id: str) -> bool:
        return self.alive.get(container_id, False)

    def list_by_label(self, label: str) -> list[str]:
        result = []
        for spec in self.create_specs:
            labels = spec.get("labels") or {}
            if label in labels:
                cid = f"cid-{self.create_specs.index(spec) + 1:04d}"
                if self.alive.get(cid, False):
                    result.append(cid)
        return result

    def stats(self, container_id: str) -> dict[str, float]:
        return self.stats_results.get(container_id, {"memory_mb": 12.5, "cpu_percent": 3.0})

    def set_alive(self, container_id: str, value: bool) -> None:
        """测试注入：模拟外部 kill -9 / 容器意外退出。"""
        self.alive[container_id] = value


def blocking_exec(seconds: float) -> ExecFn:
    """模拟一次长时间挂死的 exec（真实场景：进程 hang、daemon 无响应）。"""

    def _fn(_cid: str, _cmd: list[str], _workdir: str) -> ExecOutcome:
        time.sleep(seconds)
        return ExecOutcome(0, "", "")

    return _fn
