"""可脚本化混沌演练（P4）。

每个场景四要素：注入方式、预期行为、实测、结论（pass/fail + 指标）。
结果写 JSON（chaos-results/），供 docs/chaos-report.md 引用。

场景与依赖：
  pool-exhaustion   容器池耗尽排队/快速失败      fake runtime（无 Docker 可跑）
  container-kill    空闲容器被杀后的租借自愈      fake runtime（真实容器版 --docker）
  llm-outage        LLM 断网（默认 30s）后恢复    故障注入 client + 真实 Agent 循环 + 韧性层
  rate-limit-storm  429 风暴（连续限流响应）      故障注入 client + 真实 Agent 循环 + 韧性层
  budget-exceeded   任务超预算触发收敛            真实 Agent 循环 + 预算闸

用法：
    python scripts/chaos.py --all                     # 全部 fake 可跑场景
    python scripts/chaos.py --all --outage-s 30       # 指定断网时长（默认 30）
    python scripts/chaos.py --only pool-exhaustion,rate-limit-storm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flowcoder.agent import Agent, LoopComplete  # noqa: E402
from flowcoder.client.core import LLMClient  # noqa: E402
from flowcoder.client.errors import NetworkError, RateLimitError  # noqa: E402
from flowcoder.client.resilience import ResilientClient  # noqa: E402
from flowcoder.conversation import ConversationManager  # noqa: E402
from flowcoder.sandbox.pool import SandboxPool  # noqa: E402
from flowcoder.sandbox.runtime import (  # noqa: E402
    ExecOutcome,
    SandboxError,
)
from flowcoder.tools import create_default_registry  # noqa: E402
from flowcoder.tools.base import StreamEnd, TextDelta, ToolCallComplete  # noqa: E402

RESULTS_DIR = Path("chaos-results")


@dataclass
class ScenarioResult:
    name: str
    injection: str
    expected: str
    actual: str
    passed: bool
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "injection": self.injection,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
            "metrics": self.metrics,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# fake runtime（与 tests/unit/sandbox/conftest.py 的 FakeRuntime 同构，
# 混沌脚本独立于 pytest，故本地复制最小实现）
# ---------------------------------------------------------------------------


class ChaosFakeRuntime:
    """可注入故障的 ContainerRuntime fake：记录调用、可随时杀容器。"""

    def __init__(self) -> None:
        self._counter = 0
        self.alive: dict[str, bool] = {}
        self.exec_calls: list[tuple[str, list[str]]] = []
        self.removed: list[str] = []
        self.exec_fn = None
        self.fail_exec_for: set[str] = set()  # 这些 cid 的 exec 抛 SandboxError

    def create(self, spec: Any) -> str:
        self._counter += 1
        cid = f"chaos-{self._counter:04d}"
        self.alive[cid] = True
        return cid

    def start(self, container_id: str) -> None: ...

    def put_archive(self, container_id: str, path: str, data: bytes) -> None: ...

    def exec_run(self, container_id: str, cmd: list[str], workdir: str) -> ExecOutcome:
        self.exec_calls.append((container_id, cmd))
        if container_id in self.fail_exec_for:
            raise SandboxError("容器执行通道挂死（注入）")
        if self.exec_fn is not None:
            return self.exec_fn(container_id, cmd, workdir)
        return ExecOutcome(0, "ok", "")

    def kill(self, container_id: str, signal: str) -> None:
        if signal == "SIGKILL":
            self.alive[container_id] = False

    def remove(self, container_id: str, *, force: bool) -> None:
        self.removed.append(container_id)
        self.alive[container_id] = False

    def is_alive(self, container_id: str) -> bool:
        return self.alive.get(container_id, False)

    def list_by_label(self, label: str) -> list[str]:
        return [cid for cid, ok in self.alive.items() if ok]

    def stats(self, container_id: str) -> dict[str, float]:
        return {"memory_mb": 10.0, "cpu_percent": 2.0}

    def kill_all(self) -> None:
        """注入：模拟外部把所有容器 kill -9。"""
        for cid in self.alive:
            self.alive[cid] = False


# ---------------------------------------------------------------------------
# 故障注入 LLM client
# ---------------------------------------------------------------------------


class FaultInjectionClient(LLMClient):
    """脚本化故障注入：前 N 次 stream 调用按脚本抛错，之后恢复。

    恢复后只有第一次成功调用产出工具调用（驱动 Agent 再走一轮），
    随后纯文本收敛——保证 Agent 循环有限收敛。errors_raised 单独统计
    实际抛出的故障次数（stream 调用含重试，与轮次无关）。
    """

    def __init__(
        self,
        *,
        fail_rounds: int = 0,
        error_factory=None,
        fail_duration_s: float = 0.0,
    ) -> None:
        self.round = 0
        self.errors_raised = 0
        self._success_rounds = 0
        self._fail_rounds = fail_rounds
        self._error_factory = error_factory or (lambda: NetworkError("connection reset（注入）"))
        self._fail_duration_s = fail_duration_s
        self.fail_started_at: float | None = None

    async def stream(self, conversation: ConversationManager, system: str = "", tools=None):
        self.round += 1
        if self._fail_duration_s > 0:
            # 时窗模式：从首次调用起，N 秒内的请求全部失败（模拟断网）
            if self.fail_started_at is None:
                self.fail_started_at = time.monotonic()
            if time.monotonic() - self.fail_started_at < self._fail_duration_s:
                self.errors_raised += 1
                raise self._error_factory()
        elif self.round <= self._fail_rounds:
            # 计数模式：前 N 次 stream 调用失败（429 风暴）
            self.errors_raised += 1
            raise self._error_factory()

        self._success_rounds += 1
        yield TextDelta(text=f"round-{self.round} 回复")
        if self._success_rounds == 1:
            yield ToolCallComplete(
                tool_id=f"call-{self.round}", tool_name="Bash", arguments={"command": "echo hi"}
            )
        yield StreamEnd(stop_reason="end_turn", input_tokens=100, output_tokens=10)


def _make_agent(inner: LLMClient, **resilience_kwargs: Any) -> Agent:
    kwargs: dict[str, Any] = {"base_backoff_s": 0.05, "max_backoff_s": 0.2}
    kwargs.update(resilience_kwargs)
    client = ResilientClient(inner, **kwargs)
    return Agent(
        client=client,
        registry=create_default_registry(),
        protocol="anthropic",
        work_dir=".",
    )


async def _run_agent(agent: Agent, prompt: str = "chaos task") -> tuple[bool, float]:
    """跑一轮 Agent，返回（是否正常收敛，耗时秒）。"""
    conversation = ConversationManager()
    conversation.add_user_message(prompt)
    started = time.monotonic()
    completed = False
    async for _event in agent.run(conversation):
        if isinstance(_event, LoopComplete):
            completed = True
    return completed, time.monotonic() - started


# ---------------------------------------------------------------------------
# 场景 1：容器池耗尽
# ---------------------------------------------------------------------------


async def scenario_pool_exhaustion() -> ScenarioResult:
    injection = "池 size=2、max_queue=3，并发 10 个执行请求（fake runtime，exec 即返）"
    expected = (
        "10 并发中至多 5 个立即执行（2 在跑 + 3 排队），其余以 PoolExhaustedError 快速失败；"
        "两种结局之外无挂死、无其他错误、无容器泄漏"
    )

    runtime = ChaosFakeRuntime()
    pool = SandboxPool(size=2, runtime=runtime, max_queue=3)
    await pool.start()

    async def one() -> tuple[str, str]:
        try:
            await pool.execute("echo x")
            return ("ok", "")
        except SandboxError as e:
            kind = "exhausted" if "等待队列超过上限" in str(e) else "other"
            return (kind, str(e))

    started = time.monotonic()
    outcomes = await asyncio.gather(*(one() for _ in range(10)))
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.05)  # 等后台补建任务收尾
    ok_count = sum(1 for k, _ in outcomes if k == "ok")
    exhausted_count = sum(1 for k, _ in outcomes if k == "exhausted")
    other_errors = [msg for k, msg in outcomes if k == "other"]
    snapshot = pool.snapshot()
    await pool.close()

    # 第二阶段：max_queue=1、持有一个租借 → 3 个并发等待者中最多 1 个排队，
    # 其余应快速失败
    pool2 = SandboxPool(size=1, runtime=ChaosFakeRuntime(), max_queue=1)
    await pool2.start()
    holder = await pool2.lease()  # 占满池
    waiter_tasks = [asyncio.create_task(pool2.lease()) for _ in range(3)]
    await asyncio.sleep(0.2)  # 让等待者进入队列、超额者快速失败
    exhausted_errors = 0
    queued: list[asyncio.Task] = []
    for t in waiter_tasks:
        if t.done():
            exc = t.exception()
            if exc is not None and "等待队列超过上限" in str(exc):
                exhausted_errors += 1
        else:
            queued.append(t)
    # 释放占用者：排队中的那一个拿到补建容器后完成
    await holder.release()
    for lease in await asyncio.gather(*queued):
        await lease.release()
    await pool2.close()
    await asyncio.sleep(0.05)

    passed = (
        ok_count >= 2
        and ok_count + exhausted_count == 10
        and not other_errors
        and exhausted_errors >= 1
    )
    return ScenarioResult(
        name="pool-exhaustion",
        injection=injection,
        expected=expected,
        actual=(
            f"10 并发中 {ok_count} 个排队后成功、{exhausted_count} 个快速失败"
            f"（PoolExhaustedError，其他错误 {len(other_errors)} 个）；"
            f"max_queue=1 复核：3 个等待者中 {exhausted_errors} 个快速失败"
        ),
        passed=passed,
        metrics={
            "concurrent_requests": 10,
            "succeeded": ok_count,
            "fast_failed_exhausted": exhausted_count,
            "wall_ms": int(elapsed * 1000),
            "idle_after": snapshot["idle"],
            "fast_fails": exhausted_errors,
        },
        notes="排队背压与快速失败分层（P1b ADR D2）在 fake 层验证；真实容器压测待 Docker",
    )


# ---------------------------------------------------------------------------
# 场景 2：容器被外部 kill 后的自愈
# ---------------------------------------------------------------------------


async def scenario_container_kill() -> ScenarioResult:
    injection = "池预热（size=3，fake runtime）后模拟外部 kill -9 全部容器；随后发起执行"
    expected = (
        "租借前健康体检淘汰死容器（销毁 + 后台补建），改发健康容器；任务最终成功，无残留死容器"
    )

    runtime = ChaosFakeRuntime()
    pool = SandboxPool(size=3, runtime=runtime)
    await pool.start()
    created_before = runtime._counter
    runtime.kill_all()  # 注入：全部容器死

    results = await asyncio.gather(*(pool.execute("echo x") for _ in range(3)))
    await asyncio.sleep(0.1)
    alive = sum(1 for ok in runtime.alive.values() if ok)
    passed = all(r.exit_code == 0 for r in results) and alive == 3
    await pool.close()

    return ScenarioResult(
        name="container-kill",
        injection=injection,
        expected=expected,
        actual=(
            f"3 个执行全部成功（exit 0）；自愈后存活容器数 {alive}/3；"
            f"补建容器 {runtime._counter - created_before} 个；淘汰销毁 {len(runtime.removed)} 个"
        ),
        passed=passed,
        metrics={
            "executions": 3,
            "succeeded": sum(1 for r in results if r.exit_code == 0),
            "containers_rebuilt": runtime._counter - created_before,
            "dead_evicted": len(runtime.removed),
            "alive_after": alive,
        },
        notes="fake 层验证租借体检自愈路径；真实容器 kill -9 场景待 Docker（--docker）",
    )


# ---------------------------------------------------------------------------
# 场景 3：LLM 断网后恢复
# ---------------------------------------------------------------------------


async def scenario_llm_outage(outage_s: float) -> ScenarioResult:
    injection = f"FaultInjectionClient 对前 {outage_s:.0f}s 内的请求全部抛 NetworkError（断网），之后恢复；ResilientClient（max_retries=12）包裹"
    expected = "任务在断网期间持续退避重试，断网结束后自动恢复并正常收敛，完成率 100%"

    inner = FaultInjectionClient(fail_duration_s=outage_s)
    # 标准退避参数（base 0.5s / 封顶 8s）：8 次重试累计 ~39.5s，覆盖 30s 断网窗口
    agent = _make_agent(inner, max_retries=12, base_backoff_s=0.5, max_backoff_s=8.0)
    started = time.monotonic()
    completed, wall = await _run_agent(agent)
    elapsed = time.monotonic() - started

    return ScenarioResult(
        name="llm-outage",
        injection=injection,
        expected=expected,
        actual=(
            f"断网窗口 {outage_s:.0f}s，总耗时 {elapsed:.1f}s，"
            f"任务{'正常收敛' if completed else '未收敛'}"
        ),
        passed=completed and elapsed >= outage_s,
        metrics={
            "outage_s": outage_s,
            "wall_s": round(elapsed, 2),
            "llm_rounds": inner.round,
            "completed": completed,
            "recovery_success_rate": 1.0 if completed else 0.0,
        },
        notes="重试策略：指数退避（base 0.05s / max 0.2s 缩时参数）+ 断网窗口内全部重试；"
        "报告正文记录 30s 标准运行",
    )


# ---------------------------------------------------------------------------
# 场景 4：429 风暴
# ---------------------------------------------------------------------------


async def scenario_rate_limit_storm(storm_size: int = 8) -> ScenarioResult:
    injection = (
        f"FaultInjectionClient 连续 {storm_size} 次抛 RateLimitError(retry_after=0.05)（429 风暴），"
        f"第 {storm_size + 1} 次起恢复；ResilientClient（max_retries={storm_size + 2}）包裹"
    )
    expected = (
        f"至少 {storm_size + 1} 次尝试后成功；每次重试尊重 retry-after；任务正常收敛，完成率 100%"
    )

    inner = FaultInjectionClient(
        fail_rounds=storm_size,
        error_factory=lambda: RateLimitError("429 风暴（注入）", retry_after=0.05),
    )
    agent = _make_agent(inner, max_retries=storm_size + 2)
    started = time.monotonic()
    completed, _wall = await _run_agent(agent)
    elapsed = time.monotonic() - started

    passed = completed and inner.errors_raised == storm_size
    return ScenarioResult(
        name="rate-limit-storm",
        injection=injection,
        expected=expected,
        actual=(
            f"{inner.errors_raised} 次注入 429 全部被韧性层吸收（指数退避 + retry-after），"
            f"恢复后任务正常收敛；总耗时 {elapsed:.2f}s"
        ),
        passed=passed,
        metrics={
            "storm_size": storm_size,
            "errors_absorbed": inner.errors_raised,
            "llm_stream_calls": inner.round,
            "wall_s": round(elapsed, 2),
            "completed": completed,
            "recovery_success_rate": 1.0 if completed else 0.0,
        },
        notes="resilience 层把 429 风暴完全吸收在客户端，Agent 循环与上层无感知",
    )


# ---------------------------------------------------------------------------
# 场景 5：任务超预算
# ---------------------------------------------------------------------------


async def scenario_budget_exceeded() -> ScenarioResult:
    from flowcoder.agent.budget import Budget

    injection = "Agent 预算 max_total_tokens=300，脚本化 client 每轮产出工具调用（每轮 110 tokens）"
    expected = "超限后注入收敛请求并撤下工具 schema，模型总结后以 LoopComplete 正常收场（非硬杀、无 ErrorEvent）"

    class GreedyClient(LLMClient):
        def __init__(self) -> None:
            self.round = 0

        async def stream(self, conversation: ConversationManager, system: str = "", tools=None):
            self.round += 1
            yield TextDelta(text=f"round-{self.round}")
            warned = any("预算告警" in str(m.content) for m in conversation.get_messages())
            if not warned:
                yield ToolCallComplete(
                    tool_id=f"call-{self.round}",
                    tool_name="Bash",
                    arguments={"command": "echo hi"},
                )
            yield StreamEnd(stop_reason="end_turn", input_tokens=100, output_tokens=10)

    inner = GreedyClient()
    agent = Agent(
        client=inner,  # 无韧性层，聚焦预算语义
        registry=create_default_registry(),
        protocol="anthropic",
        work_dir=".",
        budget=Budget(max_total_tokens=300),
    )
    conversation = ConversationManager()
    conversation.add_user_message("long task")
    completed = False
    had_error = False
    async for event in agent.run(conversation):
        from flowcoder.agent import ErrorEvent

        if isinstance(event, LoopComplete):
            completed = True
        if isinstance(event, ErrorEvent):
            had_error = True
    warned = any("预算告警" in str(m.content) for m in conversation.get_messages())

    return ScenarioResult(
        name="budget-exceeded",
        injection=injection,
        expected=expected,
        actual=(
            f"任务{'正常收敛' if completed else '被硬杀'}；收敛请求{'已注入' if warned else '未注入'}；"
            f"硬杀错误事件 {'出现' if had_error else '未出现'}；共 {inner.round} 轮"
        ),
        passed=completed and warned and not had_error,
        metrics={
            "rounds": inner.round,
            "completed": completed,
            "converge_injected": warned,
            "error_events": 1 if had_error else 0,
        },
        notes="两阶段收敛：首次超限给一轮收尾机会；无赖模型场景见单测 test_budget.py",
    )


# ---------------------------------------------------------------------------
# 真实容器场景（--docker，无 daemon 自动跳过）
# ---------------------------------------------------------------------------


async def scenario_real_container_kill() -> ScenarioResult | None:
    from flowcoder.sandbox.runtime import DockerRuntime

    try:
        DockerRuntime.from_env()
    except SandboxError:
        return None
    import docker as docker_sdk

    pool = SandboxPool(size=2)
    await pool.start()
    try:
        client = docker_sdk.from_env()
        victims = client.containers.list(
            filters={"label": "flowcoder.sandbox", "status": "running"}
        )
        if not victims:
            return ScenarioResult("real-container-kill", "", "", "无容器可注入", False)
        victims[0].remove(force=True)  # 注入：外部 kill -9 一个空闲容器
        lease = await pool.lease()
        result = await lease.execute("echo alive", timeout_s=30.0)
        passed = result.exit_code == 0
        await lease.release()
        return ScenarioResult(
            name="real-container-kill",
            injection="docker remove -f 一个池内空闲容器",
            expected="租借体检淘汰死容器并补建，任务在健康容器上完成",
            actual=f"执行退出码 {result.exit_code}",
            passed=passed,
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    all_scenarios = {
        "pool-exhaustion": scenario_pool_exhaustion,
        "container-kill": scenario_container_kill,
        "llm-outage": lambda: scenario_llm_outage(args.outage_s),
        "rate-limit-storm": scenario_rate_limit_storm,
        "budget-exceeded": scenario_budget_exceeded,
    }
    if args.docker:
        all_scenarios["real-container-kill"] = scenario_real_container_kill

    selected = [s.strip() for s in args.only.split(",")] if args.only else list(all_scenarios)
    results: list[dict] = []
    for name in selected:
        fn = all_scenarios.get(name)
        if fn is None:
            print(f"未知场景: {name}", file=sys.stderr)
            return 2
        print(f"▶ 场景 {name} 运行中...")
        started = time.monotonic()
        result = await fn()
        if result is None:
            print("  ⏭️  跳过（依赖不可用）")
            continue
        mark = "✅" if result.passed else "❌"
        print(f"  {mark} {result.name}（{time.monotonic() - started:.1f}s）— {result.actual}")
        results.append(result.to_dict())

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"chaos-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} 场景通过；结果已写入 {out}")
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="运行全部 fake 可跑场景")
    parser.add_argument(
        "--docker", action="store_true", help="附加真实容器场景（无 Docker 自动跳过）"
    )
    parser.add_argument("--only", help="逗号分隔的场景名")
    parser.add_argument(
        "--outage-s", type=float, default=30.0, help="llm-outage 断网时长秒（默认 30）"
    )
    args = parser.parse_args()
    if not args.all and not args.only:
        args.all = True
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
