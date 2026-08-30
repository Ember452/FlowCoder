"""评测运行器：逐题调用 Agent 生成解法，在沙箱中执行测试（P2b）。

评测是 Agent 的消费者（PROMPTS.md 约束：不改 agent/core.py）。
数据模型与协议见 models.py，真实实现（LiveAgentSolver / DockerSandboxExecutor）
见 executors.py；本模块只保留 EvalRunner 编排逻辑（自愈闭环 + k-sample 首胜）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from flowcoder.eval.datasets import Problem, is_harness_compatible
from flowcoder.eval.executors import DockerSandboxExecutor, LiveAgentSolver  # noqa: F401
from flowcoder.eval.failure_tax import classify_failure
from flowcoder.eval.models import (  # noqa: F401
    REPAIR_PROMPT_TEMPLATE,
    SOLUTION_PROMPT_TEMPLATE,
    AgentSolution,
    ExecutionOutcome,
    ProblemResult,
    RoundRecord,
    SandboxExecutor,
    SolutionSolver,
    SolverSession,
    TrialRecord,
    build_test_harness,
    extract_code,
)

logger = logging.getLogger(__name__)


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
