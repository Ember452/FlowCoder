"""评测运行器：逐题调用 Agent 生成解法，在沙箱中执行测试。

评测是 Agent 的消费者（PROMPTS.md P2a 约束：不改 agent/core.py）：
- 生成侧依赖 Solver Protocol——LiveAgentSolver 驱动 agent.run() 事件流
  （收集 StreamText / UsageEvent / ErrorEvent），测试注入 fake；
- 执行侧依赖 SandboxExecutor Protocol——真实实现走 sandbox 模块的容器池，
  测试注入 fake（无 Docker 环境既定决策）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from flowcoder.eval.datasets import Problem, is_harness_compatible

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

#: 生成解法的用户提示模板（要求只输出完整函数，便于可靠提取）
SOLUTION_PROMPT_TEMPLATE = """[评测题目 {task_id}]
请补全以下 Python 函数。只输出完整的函数实现（含函数签名），用 ```python 代码块包裹，不要输出其他解释。

{prompt}"""


@dataclass
class AgentSolution:
    """一次求解的原始产出。"""

    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    error: str | None = None


class SolutionSolver(Protocol):
    """解法生成器抽象：LiveAgentSolver（真实 Agent 循环）或测试 fake。"""

    async def solve(self, prompt: str) -> AgentSolution: ...


@dataclass(frozen=True)
class ExecutionOutcome:
    """沙箱执行一次测试的原始结果。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class SandboxExecutor(Protocol):
    """沙箱执行抽象：DockerSandboxExecutor（容器池）或测试 fake。"""

    async def run_test(self, files: Mapping[str, str], timeout_s: float) -> ExecutionOutcome: ...


@dataclass
class ProblemResult:
    """一道题的完整评测结果。"""

    task_id: str
    passed: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0  # 沙箱执行耗时
    timed_out: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    gen_error: str | None = None  # 生成阶段错误（未到执行）
    skipped: bool = False  # special-oracle 题：轻量 harness 无法正确判定
    _generated_code: str = field(default="", repr=False)


def extract_code(text: str) -> str:
    """从模型输出提取 Python 代码：优先最后一个 ```python 围栏块，否则取全文。"""
    blocks = _CODE_FENCE_RE.findall(text)
    if blocks:
        return blocks[-1].strip()
    return text.strip()


def build_test_harness(problem: Problem, solution_code: str) -> str:
    """拼装可执行测试文件：解法 + 测试片段 + 统一追加 check(entry_point) 入口。

    HumanEval+ 的 test 片段只定义 check(candidate)，不自带调用行；
    内置玩具题遵循同一约定。
    """
    return (
        f"{solution_code.rstrip()}\n\n\n{problem.test.strip()}\n\n\ncheck({problem.entry_point})\n"
    )


class EvalRunner:
    """按题评测：生成 → 沙箱执行 → 结果。asyncio.Semaphore 限并发。"""

    def __init__(
        self,
        solver: SolutionSolver,
        executor: SandboxExecutor,
        *,
        concurrency: int = 4,
        timeout_s: float = 30.0,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency 必须为正数")
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须为正数")
        self._solver = solver
        self._executor = executor
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout_s = timeout_s

    async def run(self, problems: list[Problem]) -> list[ProblemResult]:
        """并发评测全部题目；结果顺序与输入一致。

        special-oracle 题（is_harness_compatible 为 False）不生成不执行，
        直接产出 skipped 结果，由 metrics 单列，不计入 pass@1 分母。
        """
        tasks = [self._run_one(p) for p in problems]
        return list(await asyncio.gather(*tasks))

    async def _run_one(self, problem: Problem) -> ProblemResult:
        if not is_harness_compatible(problem):
            return ProblemResult(task_id=problem.task_id, skipped=True)
        async with self._semaphore:
            return await self._evaluate(problem)

    async def _evaluate(self, problem: Problem) -> ProblemResult:
        result = ProblemResult(task_id=problem.task_id)

        # ① 生成
        solution = await self._solver.solve(
            SOLUTION_PROMPT_TEMPLATE.format(task_id=problem.task_id, prompt=problem.prompt)
        )
        result.input_tokens = solution.input_tokens
        result.output_tokens = solution.output_tokens
        result.turns = solution.turns
        if solution.error is not None:
            result.gen_error = solution.error
            return result

        # ② 提取代码并拼装测试文件
        code = extract_code(solution.text)
        result._generated_code = code
        if not code:
            result.gen_error = "模型未输出任何代码"
            return result
        files = {"run_test.py": build_test_harness(problem, code)}

        # ③ 沙箱执行
        started = time.perf_counter()
        try:
            outcome = await self._executor.run_test(files, timeout_s=self._timeout_s)
        except Exception as e:  # 执行通道故障也计入结果而非炸掉整轮评测
            logger.error("沙箱执行失败（%s）：%s", problem.task_id, e)
            result.gen_error = f"sandbox executor error: {e}"
            return result
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.exit_code = outcome.exit_code
        result.stdout = outcome.stdout
        result.stderr = outcome.stderr
        result.timed_out = outcome.timed_out
        result.passed = (not outcome.timed_out) and outcome.exit_code == 0
        return result


class LiveAgentSolver:
    """驱动真实 Agent 循环的 Solver 实现（评测是 Agent 的消费者）。"""

    def __init__(self, agent) -> None:  # agent: flowcoder.agent.Agent
        self._agent = agent

    async def solve(self, prompt: str) -> AgentSolution:
        from flowcoder.agent import ErrorEvent, LoopComplete, StreamText, UsageEvent
        from flowcoder.conversation import ConversationManager

        solution = AgentSolution()
        conversation = ConversationManager()
        conversation.add_user_message(prompt)
        async for event in self._agent.run(conversation):
            if isinstance(event, StreamText):
                solution.text += event.text
            elif isinstance(event, UsageEvent):
                solution.input_tokens += event.input_tokens
                solution.output_tokens += event.output_tokens
            elif isinstance(event, ErrorEvent):
                solution.error = event.message
            elif isinstance(event, LoopComplete):
                solution.turns = event.total_turns
        return solution


class DockerSandboxExecutor:
    """基于 sandbox 模块容器池的执行器（真实 Docker 路径）。"""

    def __init__(self, *, image: str = "python:3.11-slim", pool_size: int = 2) -> None:
        from flowcoder.sandbox import SandboxConfig

        self._config = SandboxConfig(image=image)
        self._pool_size = pool_size
        self._pool = None

    async def _ensure_pool(self):
        from flowcoder.sandbox import SandboxPool

        if self._pool is None:
            pool = SandboxPool(size=self._pool_size, config=self._config)
            await pool.start()
            self._pool = pool
        return self._pool

    async def run_test(self, files: Mapping[str, str], timeout_s: float) -> ExecutionOutcome:
        from flowcoder.sandbox import SandboxError

        try:
            pool = await self._ensure_pool()
            result = await pool.execute(
                ["python", "run_test.py"], files=dict(files), timeout_s=timeout_s
            )
        except SandboxError as e:
            raise RuntimeError(f"沙箱不可用：{e}") from e
        return ExecutionOutcome(
            exit_code=result.exit_code if result.exit_code is not None else -1,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )
