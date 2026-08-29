"""EvalRunner 全链路测试：内置玩具题 + fake provider（不依赖真实 LLM / Docker）。

执行通道用 LocalPythonExecutor（python -c 真跑 harness，无文件写入、无网络），
既验证 runner 编排逻辑，也验证 harness 拼装与代码提取的正确性。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from flowcoder.eval.datasets import BUILTIN_TOY_PROBLEMS, Problem
from flowcoder.eval.runner import (
    AgentSolution,
    EvalRunner,
    ExecutionOutcome,
    build_test_harness,
    extract_code,
)


class FakeSolver:
    """fake provider：会话式，按 task_id 返回预设输出。

    answers 值可以是 str（每次 ask 同答案）或 list[str]（按 ask 次序轮替，
    用尽后重复最后一项）——支撑自愈轮与 k-sample 的测试。
    """

    def __init__(self, answers: dict[str, str | list[str]]) -> None:
        self._answers = answers
        self.prompts: list[str] = []
        self.active_sessions = 0

    def start(self) -> FakeSession:
        return FakeSession(self)


class FakeSession:
    def __init__(self, solver: FakeSolver) -> None:
        self._solver = solver
        self._ask_count = 0
        solver.active_sessions += 1

    async def ask(self, prompt: str) -> AgentSolution:
        self._solver.prompts.append(prompt)
        self._ask_count += 1
        text = ""
        for key, value in self._solver._answers.items():
            if key in prompt:
                if isinstance(value, list):
                    text = value[min(self._ask_count, len(value)) - 1]
                else:
                    text = value
                break
        return AgentSolution(text=text, input_tokens=100, output_tokens=20, turns=1)

    def close(self) -> None:
        self._solver.active_sessions -= 1


class ErrorSolver:
    def start(self) -> ErrorSession:
        return ErrorSession()


class ErrorSession:
    async def ask(self, prompt: str) -> AgentSolution:
        return AgentSolution(error="LLM 429 限流")


class LocalPythonExecutor:
    """python -c 真执行 harness 的 fake 沙箱（无 Docker 依赖）。"""

    def __init__(self) -> None:
        self.concurrent = 0
        self.max_concurrent = 0

    async def run_test(self, files: dict, timeout_s: float) -> ExecutionOutcome:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                files["run_test.py"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionOutcome(exit_code=-1, timed_out=True)
        finally:
            self.concurrent -= 1
        return ExecutionOutcome(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )


_RIGHT_ADD = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
_WRONG_ADD = "```python\ndef add(a: int, b: int) -> int:\n    return a + b + 1\n```"
_RIGHT_PALINDROME = "```python\ndef is_palindrome(s: str) -> bool:\n    return s == s[::-1]\n```"


class TestExtractCode:
    def test_fenced_block(self) -> None:
        assert extract_code("说明\n```python\nx = 1\n```") == "x = 1"

    def test_multiple_blocks_takes_last(self) -> None:
        text = "```python\na = 1\n```\n中间文本\n```\nb = 2\n```"
        assert extract_code(text) == "b = 2"

    def test_bare_code_without_fence(self) -> None:
        assert extract_code("def f():\n    pass") == "def f():\n    pass"

    def test_empty(self) -> None:
        assert extract_code("   ") == ""


class TestBuildHarness:
    def test_concatenates_and_appends_entry(self) -> None:
        p = BUILTIN_TOY_PROBLEMS[0]
        harness = build_test_harness(p, "def add(a, b):\n    return a + b")
        assert "def add" in harness
        assert "def check(candidate)" in harness
        assert harness.rstrip().endswith("check(add)")


class TestFullChain:
    async def test_both_toy_problems_pass(self) -> None:
        solver = FakeSolver({"toy/add": _RIGHT_ADD, "toy/palindrome": _RIGHT_PALINDROME})
        runner = EvalRunner(solver, LocalPythonExecutor(), concurrency=2, timeout_s=15)
        results = await runner.run(list(BUILTIN_TOY_PROBLEMS))
        assert [r.task_id for r in results] == ["toy/add", "toy/palindrome"]
        assert all(r.passed for r in results)
        assert all(r.exit_code == 0 for r in results)
        assert all(r.input_tokens == 100 and r.output_tokens == 20 for r in results)
        # 提示词包含题目 prompt
        assert "def add" in solver.prompts[0]

    async def test_wrong_solution_fails(self) -> None:
        solver = FakeSolver({"toy/add": _WRONG_ADD, "toy/palindrome": _RIGHT_PALINDROME})
        runner = EvalRunner(solver, LocalPythonExecutor(), timeout_s=15)
        results = await runner.run(list(BUILTIN_TOY_PROBLEMS))
        by_id = {r.task_id: r for r in results}
        assert not by_id["toy/add"].passed
        assert by_id["toy/add"].exit_code != 0
        assert by_id["toy/palindrome"].passed

    async def test_generation_error_short_circuits(self) -> None:
        runner = EvalRunner(ErrorSolver(), LocalPythonExecutor())
        results = await runner.run(list(BUILTIN_TOY_PROBLEMS))
        assert all(r.gen_error == "LLM 429 限流" for r in results)
        assert all(not r.passed for r in results)
        assert all(r.exit_code is None for r in results)

    async def test_prose_answer_fails_execution(self) -> None:
        # 无围栏的散文走全文兜底 → 执行报语法错误 → 计为未通过
        solver = FakeSolver({"toy/add": "抱歉，我无法回答。"})
        runner = EvalRunner(solver, LocalPythonExecutor(), timeout_s=15)
        results = await runner.run([BUILTIN_TOY_PROBLEMS[0]])
        assert results[0].gen_error is None
        assert not results[0].passed
        assert results[0].exit_code != 0

    async def test_blank_output_is_gen_error(self) -> None:
        solver = FakeSolver({"toy/add": "  "})
        runner = EvalRunner(solver, LocalPythonExecutor())
        results = await runner.run([BUILTIN_TOY_PROBLEMS[0]])
        assert results[0].gen_error == "模型未输出任何代码"

    async def test_executor_crash_recorded_not_raised(self) -> None:
        class BoomExecutor:
            async def run_test(self, files: dict, timeout_s: float) -> ExecutionOutcome:
                raise RuntimeError("daemon 挂了")

        runner = EvalRunner(FakeSolver({"toy/add": _RIGHT_ADD}), BoomExecutor())
        results = await runner.run([BUILTIN_TOY_PROBLEMS[0]])
        assert results[0].gen_error is not None
        assert "sandbox executor error" in results[0].gen_error
        assert not results[0].passed

    async def test_semaphore_limits_concurrency(self) -> None:
        class SlowSession:
            def __init__(self, owner: "SlowSolver") -> None:
                self._owner = owner

            async def ask(self, prompt: str) -> AgentSolution:
                owner = self._owner
                owner.concurrent += 1
                owner.max_concurrent = max(owner.max_concurrent, owner.concurrent)
                await asyncio.sleep(0.05)
                owner.concurrent -= 1
                return AgentSolution(text=_RIGHT_ADD)

        class SlowSolver:
            def __init__(self) -> None:
                self.concurrent = 0
                self.max_concurrent = 0

            def start(self) -> SlowSession:
                return SlowSession(self)

        problems = [BUILTIN_TOY_PROBLEMS[0]] * 6
        solver = SlowSolver()
        runner = EvalRunner(solver, LocalPythonExecutor(), concurrency=2, timeout_s=15)
        results = await runner.run(problems)
        assert len(results) == 6
        assert solver.max_concurrent <= 2

    async def test_special_oracle_skipped_without_generation(self) -> None:
        # special-oracle 题（test 含 *candidate( 解包变换）跳过生成与执行
        special = Problem(
            task_id="HumanEval/32",
            prompt="def find_zero(xs):\n",
            test="def check(candidate):\n    assert _poly(*candidate(*inp), inp)\n",
            entry_point="find_zero",
        )
        solver = FakeSolver({"find_zero": _RIGHT_ADD, "toy/add": _RIGHT_ADD})
        executor = LocalPythonExecutor()
        runner = EvalRunner(solver, executor, timeout_s=15)
        results = await runner.run([special, BUILTIN_TOY_PROBLEMS[0]])
        assert results[0].skipped
        assert not results[0].passed
        assert results[0].input_tokens == 0  # 未发生生成
        assert executor.max_concurrent == 1  # 只有 toy/add 真正执行
        assert results[1].passed  # 正常题不受影响

    async def test_invalid_construction(self) -> None:
        solver = FakeSolver({})
        executor = LocalPythonExecutor()
        with pytest.raises(ValueError):
            EvalRunner(solver, executor, concurrency=0)
        with pytest.raises(ValueError):
            EvalRunner(solver, executor, timeout_s=0)
