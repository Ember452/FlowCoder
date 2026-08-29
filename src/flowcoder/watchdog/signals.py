"""看门狗信号源：可插拔接口 + 三种内置实现（P5b）。

信号是"值得看一眼的仓库事件"的统一抽象，`delivery_key` 是防骚扰去重的
锚点——同一 key 只会被提示一次（即使跨重启）。key 必须刻画事件的**内容**
而非时间（同一份脏工作区反复 poll 产出同一个 key，被门控去重；状态变化
产出新 key，才会再次进入判定）。

接口为 Protocol：`poll()` 返回本轮新发现的信号（实现方自己保证同源不重复
吐同一个 key，避免每轮重复消耗 LLM 判定）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Signal:
    kind: str  # git-dirty / tests-degraded / file-changed
    delivery_key: str  # 防骚扰去重锚点：内容刻画，同一事件恒定
    summary: str  # 给判定/送达的人类可读摘要
    detected_at: float


class SignalSource(Protocol):
    """可插拔信号源。poll() 返回本轮新信号（不与已 poll 过的重复）。"""

    async def poll(self) -> list[Signal]: ...


def _key(kind: str, content: str) -> str:
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]  # noqa: S324
    return f"{kind}:{digest}"


class GitStatusSource:
    """git 工作区状态变更：脏文件集合或分支变化时发信号。"""

    def __init__(self, repo_dir: str, *, command_runner=None) -> None:
        self._repo_dir = repo_dir
        self._last_key: str | None = None
        # command_runner 可注入（测试不打真实 git）；默认真实 subprocess
        self._run = command_runner or self._run_git

    @staticmethod
    async def _run_git(args: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败: {err.decode(errors='replace')[:200]}")
        return out.decode("utf-8", errors="replace")

    async def poll(self) -> list[Signal]:
        branch = (await self._run(["rev-parse", "--abbrev-ref", "HEAD"])).strip()
        status = await self._run(["status", "--porcelain"])
        lines = [ln for ln in status.splitlines() if ln.strip()]
        if not lines:
            key = f"git-clean:{branch}"
        else:
            key = _key("git-dirty", branch + "\n" + "\n".join(sorted(lines)))
        if key == self._last_key:
            return []
        self._last_key = key
        summary = (
            f"git 工作区干净（branch={branch}）"
            if not lines
            else f"git 工作区有 {len(lines)} 处变更（branch={branch}）：" + "；".join(lines[:5])
        )
        return [
            Signal(kind="git-status", delivery_key=key, summary=summary, detected_at=time.time())
        ]


class TestResultsSource:
    """测试结果恶化：pass 率相比上一次记录下降时发信号。

    结果由外部（CI 轮询 / 本地跑测后）经 report() 喂入，不做真实跑测。
    """

    def __init__(self) -> None:
        self._last_pass_rate: float | None = None

    def report(self, label: str, passed: int, total: int) -> Signal | None:
        if total <= 0:
            return None
        rate = passed / total
        previous = self._last_pass_rate
        self._last_pass_rate = rate
        if previous is None or rate >= previous:
            return None  # 持平或改善不提示
        key = _key(
            "tests-degraded",
            json.dumps({"label": label, "from": round(previous, 4), "to": round(rate, 4)}),
        )
        return Signal(
            kind="tests-degraded",
            delivery_key=key,
            summary=f"测试通过率恶化（{label}）：{previous:.1%} → {rate:.1%}（{passed}/{total}）",
            detected_at=time.time(),
        )

    async def poll(self) -> list[Signal]:
        return []  # 结果由 report() 主动喂入


class FileChangeSource:
    """指定文件变更：内容 hash 变化时发信号（同一内容不重发）。"""

    def __init__(self, paths: list[str]) -> None:
        self._paths = paths
        self._hashes: dict[str, str] = {}

    async def poll(self) -> list[Signal]:
        signals: list[Signal] = []
        for path in self._paths:
            try:
                content = Path(path).read_bytes()
            except OSError:
                continue
            digest = hashlib.sha1(content).hexdigest()[:12]  # noqa: S324
            if self._hashes.get(path) == digest:
                continue
            changed = path in self._hashes  # 首次快照不算"变更"
            self._hashes[path] = digest
            if changed:
                signals.append(
                    Signal(
                        kind="file-changed",
                        delivery_key=f"file-changed:{path}:{digest}",
                        summary=f"监控文件发生变更：{path}",
                        detected_at=time.time(),
                    )
                )
        return signals
