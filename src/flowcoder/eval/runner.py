"""评测运行器：逐题调用 Agent 生成解法，在沙箱中执行测试（P2b）。

评测是 Agent 的消费者（PROMPTS.md 约束：不改 agent/core.py）：
- 生成侧依赖 SolutionSolver Protocol——会话式接口（start() → ask()），
  自愈修复轮在同一会话中追问；LiveAgentSolver 每个会话经 agent_factory
  创建独立 Agent 实例（k-sample 并行 trial 不能共享 Agent 实例状态）；
- 执行侧依赖 SandboxExecutor Protocol——真实实现走 sandbox 容器池，
  测试注入 fake（无 Docker 环境既定决策）。

自愈闭环：测试失败把 stderr/stdout 喂回 Agent（REPAIR_PROMPT_TEMPLATE），
最多 heal_rounds 轮修复重跑；超时无有效反馈不重试。
k-sample 首胜：同一题并行 k 个独立 trial，首个通过即胜出，其余 cancel；
取消语义遵守 AGENTS.md 第五节——CancelledError 放行，用
gather(return_exceptions=True) 收尾清理，不吞异常。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable, Protocol

from flowcoder.eval.datasets import Problem, is_harness_compatible
from flowcoder.eval.failure_tax import classify_failure

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

#: 生成解法的用户提示模板（要求只输出完整函数，便于可靠提取）
SOLUTION_PROMPT_TEMPLATE = """[评测题目 {task_id}]
请补全以下 Python 函数。只输出完整的函数实现（含函数签名），用 ```python 代码块包裹，不要输出其他解释。

{prompt}"""

#: 自愈修复轮提示：把失败输出喂回，要求输出修复后的完整函数
REPAIR_PROMPT_TEMPLATE = """[评测题目 {task_id}] 你上一版的解法没有通过测试，请修复。

测试输出：
{feedback}

请重新输出修复后的完整函数实现（含函数签名），用 ```python 代码块包裹，不要输出其他解释。

原题：
{prompt}"""


@dataclass
class AgentSolution:
    """一次求解的原始产出。"""

    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    error: str | None = None


class SolverSession(Protocol):
    """一次求解会话：修复轮在同一会话内追问（Agent 记得自己此前的解法）。"""

    async def ask(self, prompt: str) -> AgentSolution: ...


class SolutionSolver(Protocol):
    """解法生成器抽象。start() 开启独立会话（k-sample 的 trial 互不共享状态）。"""

    def start(self) -> SolverSession: ...


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
class RoundRecord:
    """一次生成 + 执行的记录（自愈的一轮）。"""

    round: int
    input_tokens: int = 0
    output_tokens: int = 0
    passed: bool = False
    exit_code: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    error: str | None = None  # 生成阶段错误（该轮未执行）


@dataclass
class TrialRecord:
    """一个独立 trial（k-sample 的一路）的完整记录。"""

    index: int
    rounds: list[RoundRecord] = field(default_factory=list)
    passed: bool = False
    cancelled: bool = False
    generated_code: str = ""

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.rounds)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.rounds)

    @property
    def rounds_used(self) -> int:
        return len(self.rounds)


@dataclass
class ProblemResult:
    """一道题的完整评测结果。"""

    task_id: str
    passed: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0  # 胜出（或最后失败）trial 的最终执行耗时
    timed_out: bool = False
    input_tokens: int = 0  # 全部 trial 已记录轮次合计
    output_tokens: int = 0
    turns: int = 0  # 胜出（或最后）trial 的总轮次
    gen_error: str | None = None  # 生成阶段错误（未到执行）
    skipped: bool = False  # special-oracle 题：轻量 harness 无法正确判定
    healed: bool = False  # 首轮失败、自愈轮通过
    trials_launched: int = 1
    trials_cancelled: int = 0
    failure_category: str | None = None  # 最终失败时的四分类（failure_tax）
    _trials: list[TrialRecord] = field(default_factory=list, repr=False)

    @property
    def trials(self) -> list[TrialRecord]:
        """逐 trial 明细（只读视图）。"""
        return self._trials

    @property
    def rounds_used(self) -> int:
        """胜出（或最后失败）trial 的轮数。"""
        if not self._trials:
            return 0
        return self._trials[-1].rounds_used

    @property
    def generated_code(self) -> str:
        """胜出（或最后失败）trial 的最终代码。"""
        if not self._trials:
            return ""
        return self._trials[-1].generated_code


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


def _feedback(outcome: ExecutionOutcome, limit: int = 2000) -> str:
    text = f"exit_code={outcome.exit_code}\nSTDERR:\n{outcome.stderr}\nSTDOUT:\n{outcome.stdout}"
    return text[:limit]


class EvalRunner:
    """按题评测：生成 → 沙箱执行 →（失败则自愈重试）→ k-sample 首胜。"""

    def __init__(
        self,
        solver: SolutionSolver,
        executor: SandboxExecutor,
        *,
        concurrency: int = 4,
        timeout_s: float = 30.0,
        heal_rounds: int = 0,
        k: int = 1,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency 必须为正数")
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须为正数")
        if heal_rounds < 0:
            raise ValueError("heal_rounds 不能为负数")
        if k <= 0:
            raise ValueError("k 必须为正数")
        self._solver = solver
        self._executor = executor
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout_s = timeout_s
        self._heal_rounds = heal_rounds
        self._k = k

    @property
    def heal_rounds(self) -> int:
        return self._heal_rounds

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
            if self._k == 1:
                return await self._evaluate_single(problem)
            return await self._evaluate_k_sample(problem)

    # ------------------------------------------------------------- 单 trial

    async def _evaluate_single(self, problem: Problem) -> ProblemResult:
        trial = await self._trial(problem, 0)
        return self._problem_result_from_trials(problem, [trial])

    async def _trial(self, problem: Problem, index: int) -> TrialRecord:
        """一个独立 trial：1 次生成 + 最多 heal_rounds 次修复重跑。"""
        session = self._solver.start()
        trial = TrialRecord(index=index)
        last_outcome: ExecutionOutcome | None = None

        for round_no in range(1, self._heal_rounds + 2):
            if round_no == 1:
                prompt = SOLUTION_PROMPT_TEMPLATE.format(
                    task_id=problem.task_id, prompt=problem.prompt
                )
            else:
                assert last_outcome is not None
                prompt = REPAIR_PROMPT_TEMPLATE.format(
                    task_id=problem.task_id,
                    feedback=_feedback(last_outcome),
                    prompt=problem.prompt,
                )
            record = RoundRecord(round=round_no)

            solution = await session.ask(prompt)
            record.input_tokens = solution.input_tokens
            record.output_tokens = solution.output_tokens
            if solution.error is not None:
                record.error = solution.error
                trial.rounds.append(record)
                break

            code = extract_code(solution.text)
            trial.generated_code = code
            if not code:
                record.error = "模型未输出任何代码"
                trial.rounds.append(record)
                break

            files = {"run_test.py": build_test_harness(problem, code)}
            started = time.perf_counter()
            try:
                outcome = await self._executor.run_test(files, timeout_s=self._timeout_s)
            except Exception as e:  # 执行通道故障计入该轮而非炸掉整轮评测
                logger.error("沙箱执行失败（%s）：%s", problem.task_id, e)
                record.error = f"sandbox executor error: {e}"
                trial.rounds.append(record)
                break
            record.duration_ms = int((time.perf_counter() - started) * 1000)
            record.exit_code = outcome.exit_code
            record.timed_out = outcome.timed_out
            record.passed = (not outcome.timed_out) and outcome.exit_code == 0
            last_outcome = outcome
            trial.rounds.append(record)
            if record.passed:
                break
            if outcome.timed_out:
                break  # 超时无有效失败信息，喂回没有意义

        trial.passed = trial.rounds and trial.rounds[-1].passed
        return trial

    # ------------------------------------------------------------- k-sample

    async def _evaluate_k_sample(self, problem: Problem) -> ProblemResult:
        """并行 k 个独立 trial，首个通过即胜出，其余 cancel。"""
        tasks = {
            asyncio.create_task(self._trial(problem, i), name=f"trial-{problem.task_id}-{i}"): i
            for i in range(self._k)
        }
        pending: set[asyncio.Task[TrialRecord]] = set(tasks)
        winner: TrialRecord | None = None
        finished: list[TrialRecord] = []
        cancelled = 0

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    cancelled += 1
                    continue
                exc = task.exception()
                if exc is not None:
                    # _trial 内部已兜住常规错误；这里只兜真·意外，视为失败 trial
                    logger.error("trial 意外失败（%s）：%s", problem.task_id, exc)
                    finished.append(TrialRecord(index=tasks[task], passed=False))
                    continue
                record = task.result()
                finished.append(record)
                if record.passed and winner is None:
                    winner = record
                    # 首胜即收：取消其余 trial 并等它们收尾（CancelledError 放行，
                    # gather(return_exceptions=True) 只做汇总清理不吞传播路径）
                    for p in pending:
                        p.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                        cancelled += len(pending)
                    pending = set()
                    break

        trials = [t for t in finished if not t.cancelled]
        trials.sort(key=lambda t: t.index)
        result = self._problem_result_from_trials(problem, trials)
        result.trials_launched = self._k
        result.trials_cancelled = cancelled
        return result

    # -------------------------------------------------------------- 汇总

    def _problem_result_from_trials(
        self, problem: Problem, trials: list[TrialRecord]
    ) -> ProblemResult:
        result = ProblemResult(task_id=problem.task_id, _trials=trials)
        if not trials:
            return result
        result.input_tokens = sum(t.input_tokens for t in trials)
        result.output_tokens = sum(t.output_tokens for t in trials)

        winner = next((t for t in trials if t.passed), None)
        final = winner if winner is not None else trials[-1]
        last_round = final.rounds[-1] if final.rounds else None
        result.passed = final.passed
        result.turns = final.rounds_used
        if last_round is not None:
            result.exit_code = last_round.exit_code
            result.duration_ms = last_round.duration_ms
            result.timed_out = last_round.timed_out
            gen_err = last_round.error
            result.gen_error = gen_err if not final.passed else None
        if final.rounds:
            first = final.rounds[0]
            result.healed = (not first.passed) and final.passed

        if not result.passed and result.gen_error is None:
            # heal_rounds=0 时不存在"修复预算耗尽"的概念
            rounds_exhausted = self._heal_rounds > 0 and (
                final.rounds_used >= self._heal_rounds + 1
            )
            result.failure_category = classify_failure(result, rounds_exhausted=rounds_exhausted)
        return result


class _LiveSession:
    """会话式驱动真实 Agent 循环：同一会话内多轮 ask（自愈修复共用上下文）。"""

    def __init__(self, agent: object) -> None:
        from flowcoder.conversation import ConversationManager

        self._agent = agent
        self._conversation = ConversationManager()

    async def ask(self, prompt: str) -> AgentSolution:
        from flowcoder.agent import ErrorEvent, LoopComplete, StreamText, UsageEvent

        solution = AgentSolution()
        self._conversation.add_user_message(prompt)
        async for event in self._agent.run(self._conversation):
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


class LiveAgentSolver:
    """真实 Agent 的 Solver：每个会话经 agent_factory 创建独立 Agent 实例。

    k-sample 的 trial 并行运行，Agent 实例携带可变状态（recovery_state、
    token 计数等），不能共享——工厂按会话建实例，client/registry 可复用。
    """

    def __init__(self, agent_factory: Callable[[], object]) -> None:
        self._agent_factory = agent_factory

    def start(self) -> SolverSession:
        return _LiveSession(self._agent_factory())


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
