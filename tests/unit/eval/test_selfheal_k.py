"""P2b：自愈闭环、k-sample 首胜与失败四分类测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from flowcoder.eval.datasets import BUILTIN_TOY_PROBLEMS
from flowcoder.eval.failure_tax import (
    FAIL_BUDGET,
    FAIL_COMPILE,
    FAIL_LOGIC,
    FAIL_SPEC,
    classify_failure,
)
from flowcoder.eval.metrics import compute_metrics
from flowcoder.eval.report import write_comparison_report
from flowcoder.eval.runner import (
    AgentSolution,
    EvalRunner,
    ExecutionOutcome,
    ProblemResult,
    RoundRecord,
    TrialRecord,
)

from test_runner import FakeSolver, LocalPythonExecutor, _RIGHT_ADD, _WRONG_ADD

_TOY = BUILTIN_TOY_PROBLEMS[0]  # toy/add
_PALINDROME = BUILTIN_TOY_PROBLEMS[1]


class FailingExecutor:
    """让任何解法都执行失败的 fake：非零退出 + AssertionError 特征。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run_test(self, files: dict, timeout_s: float) -> ExecutionOutcome:
        self.calls += 1
        return ExecutionOutcome(exit_code=1, stderr="AssertionError: assert 5 == 4")


class TestSelfHeal:
    async def test_repair_round_recovers(self) -> None:
        # 第 1 轮错解，第 2 轮修复 → healed=True，执行了 2 轮
        solver = FakeSolver({"toy/add": [_WRONG_ADD, _RIGHT_ADD]})
        runner = EvalRunner(solver, LocalPythonExecutor(), heal_rounds=3, timeout_s=15)
        results = await runner.run([_TOY])
        r = results[0]
        assert r.passed
        assert r.healed
        assert r.rounds_used == 2
        assert r.trials[0].input_tokens == 200  # 两轮 token 合计
        # 修复轮提示包含失败输出与原题
        assert "exit_code" in solver.prompts[1]
        assert "def add" in solver.prompts[1]
        assert "你上一版的解法没有通过测试" in solver.prompts[1]

    async def test_rounds_capped_at_limit(self) -> None:
        # 一直错：最多 1 + heal_rounds 轮
        solver = FakeSolver({"toy/add": _WRONG_ADD})
        runner = EvalRunner(solver, FailingExecutor(), heal_rounds=2)
        results = await runner.run([_TOY])
        r = results[0]
        assert not r.passed
        assert r.rounds_used == 3
        assert len(solver.prompts) == 3
        assert r.failure_category == FAIL_BUDGET  # 轮次耗尽 → 超预算

    async def test_heal_disabled_single_round(self) -> None:
        solver = FakeSolver({"toy/add": _WRONG_ADD})
        runner = EvalRunner(solver, FailingExecutor(), heal_rounds=0)
        results = await runner.run([_TOY])
        assert results[0].rounds_used == 1
        assert not results[0].healed

    async def test_timeout_not_repaired(self) -> None:
        # 超时没有有效失败信息，不应触发修复轮

        class TimeoutExecutor:
            async def run_test(self, files: dict, timeout_s: float) -> ExecutionOutcome:
                return ExecutionOutcome(exit_code=-1, timed_out=True)

        solver = FakeSolver({"toy/add": _WRONG_ADD})
        runner = EvalRunner(solver, TimeoutExecutor(), heal_rounds=3)
        results = await runner.run([_TOY])
        assert results[0].rounds_used == 1
        assert results[0].failure_category == FAIL_BUDGET

    async def test_gen_error_no_category(self) -> None:
        solver = FakeSolver({})  # 无匹配答案 → 空文本 → 生成错误
        runner = EvalRunner(solver, LocalPythonExecutor())
        results = await runner.run([_TOY])
        assert results[0].gen_error == "模型未输出任何代码"
        assert results[0].failure_category is None

    async def test_heal_metrics(self) -> None:
        # toy/add 首轮失败第 2 轮修复成功（heal=2）；palindrome 单轮断言失败（heal=0）
        solver_heal = FakeSolver({"toy/add": [_WRONG_ADD, _RIGHT_ADD]})
        runner_heal = EvalRunner(solver_heal, LocalPythonExecutor(), heal_rounds=2, timeout_s=15)
        heal_results = await runner_heal.run([_TOY])

        solver_plain = FakeSolver(
            {"toy/palindrome": "```\ndef is_palindrome(s): return False\n```"}
        )
        runner_plain = EvalRunner(solver_plain, LocalPythonExecutor(), heal_rounds=0, timeout_s=15)
        plain_results = await runner_plain.run([_PALINDROME])

        metrics = compute_metrics(heal_results + plain_results)
        assert metrics["first_round_failures"] == 2
        assert metrics["healed"] == 1
        assert metrics["heal_recovery_rate"] == 0.5
        assert metrics["fail_逻辑错"] == 1  # palindrome: False → 断言失败
        assert metrics["fail_超预算"] == 0


class IndexedSolver:
    """按会话序号返回不同解法：idx 0 立即正确，其余慢（供取消测试）。"""

    def __init__(self, slow_delay: float = 30.0) -> None:
        self._n = 0
        self._slow_delay = slow_delay

    def start(self) -> "IndexedSolver.Session":
        self._n += 1
        return self.Session(self._n - 1, self._slow_delay)

    class Session:
        def __init__(self, idx: int, slow_delay: float) -> None:
            self._idx = idx
            self._slow_delay = slow_delay

        async def ask(self, prompt: str) -> AgentSolution:
            if self._idx == 0:
                return AgentSolution(text=_RIGHT_ADD, input_tokens=1, output_tokens=1, turns=1)
            await asyncio.sleep(self._slow_delay)  # 等待被取消
            raise AssertionError("不应到达")


class TestKSampleFirstWin:
    async def test_first_win_cancels_rest(self) -> None:
        runner = EvalRunner(
            IndexedSolver(), LocalPythonExecutor(), concurrency=4, k=3, timeout_s=15
        )
        results = await runner.run([_TOY])
        r = results[0]
        assert r.passed
        assert r.trials_launched == 3
        assert r.trials_cancelled == 2
        assert r.input_tokens == 1  # 只统计到胜者（取消者未完成轮不可观测）

    async def test_cancel_leaves_no_leftover_tasks(self) -> None:
        runner = EvalRunner(
            IndexedSolver(), LocalPythonExecutor(), concurrency=4, k=3, timeout_s=15
        )
        await runner.run([_TOY])
        await asyncio.sleep(0.05)
        leftover = [t for t in asyncio.all_tasks() if "trial-" in t.get_name() and not t.done()]
        assert leftover == []

    async def test_all_fail_no_cancel(self) -> None:
        # k=3 且全部失败：没有胜者，全部自然完成
        solver = FakeSolver({"toy/add": _WRONG_ADD})
        runner = EvalRunner(solver, LocalPythonExecutor(), concurrency=4, k=3, timeout_s=15)
        results = await runner.run([_TOY])
        assert not results[0].passed
        assert results[0].trials_cancelled == 0
        assert results[0].trials_launched == 3
        assert results[0].input_tokens == 300  # 3 个 trial 各 100


class TestFailureTax:
    @staticmethod
    def _result(
        *,
        stderr: str = "",
        timed_out: bool = False,
        gen_error: str | None = None,
        code: str = "",
        skipped: bool = False,
    ) -> ProblemResult:
        r = ProblemResult(task_id="x", stderr=stderr, timed_out=timed_out, gen_error=gen_error)
        if code:
            r._trials = [TrialRecord(index=0, generated_code=code)]
        if skipped:
            r.skipped = True
        return r

    def test_timeout_is_budget(self) -> None:
        assert classify_failure(self._result(timed_out=True), rounds_exhausted=False) == FAIL_BUDGET

    def test_rounds_exhausted_is_budget(self) -> None:
        r = self._result(stderr="AssertionError: x")
        assert classify_failure(r, rounds_exhausted=True) == FAIL_BUDGET

    def test_uncompilable_code_is_compile(self) -> None:
        assert (
            classify_failure(self._result(code="def f(:\n"), rounds_exhausted=False) == FAIL_COMPILE
        )

    def test_stderr_syntax_error_is_compile(self) -> None:
        r = self._result(stderr='File "run_test.py", line 1\nSyntaxError: invalid syntax')
        assert classify_failure(r, rounds_exhausted=False) == FAIL_COMPILE

    def test_assertion_error_is_logic(self) -> None:
        r = self._result(stderr="AssertionError: assert 5 == 4")
        assert classify_failure(r, rounds_exhausted=False) == FAIL_LOGIC

    def test_type_error_is_spec_misread(self) -> None:
        r = self._result(stderr="TypeError: 'int' object is not iterable")
        assert classify_failure(r, rounds_exhausted=False) == FAIL_SPEC

    def test_other_builtin_exception_is_logic(self) -> None:
        r = self._result(stderr="ZeroDivisionError: division by zero")
        assert classify_failure(r, rounds_exhausted=False) == FAIL_LOGIC

    def test_silent_failure_falls_back_to_logic(self) -> None:
        assert classify_failure(self._result(stderr=""), rounds_exhausted=False) == FAIL_LOGIC

    def test_gen_error_and_skipped_unclassified(self) -> None:
        assert classify_failure(self._result(gen_error="LLM 429"), rounds_exhausted=False) is None
        assert classify_failure(self._result(skipped=True), rounds_exhausted=False) is None

    async def test_runner_assigns_category_on_failure(self) -> None:
        # 语法错误的解法（无修复机会）→ 编译错
        solver = FakeSolver({"toy/add": "```python\ndef add(a, b:\n```"})
        runner = EvalRunner(solver, LocalPythonExecutor(), heal_rounds=0, timeout_s=15)
        results = await runner.run([_TOY])
        assert not results[0].passed
        assert results[0].failure_category == FAIL_COMPILE


class TestComparisonReport:
    def test_comparison_markdown_and_json(self, tmp_path: Path) -> None:
        ok = ProblemResult(task_id="toy/add", passed=True, exit_code=0)
        bad = ProblemResult(task_id="toy/add", passed=False, exit_code=1)
        md_path, json_path = write_comparison_report(
            {"k=1,heal=0": compute_metrics([bad]), "k=1,heal=3": compute_metrics([ok])},
            {"k=1,heal=0": [bad], "k=1,heal=3": [ok]},
            {"temperature": "0.0"},
            tmp_path,
        )
        md = md_path.read_text(encoding="utf-8")
        assert "k=1,heal=0" in md and "k=1,heal=3" in md
        assert "逐题通过矩阵" in md
        assert "❌" in md and "✅" in md
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert set(payload["runs"]) == {"k=1,heal=0", "k=1,heal=3"}
        assert payload["runs"]["k=1,heal=3"]["pass_at_1"] == 1.0

    def test_skipped_shown_in_matrix(self, tmp_path: Path) -> None:
        skipped = ProblemResult(task_id="s/1", skipped=True)
        md_path, _ = write_comparison_report(
            {"run": compute_metrics([skipped])}, {"run": [skipped]}, {}, tmp_path
        )
        assert "⏭️" in md_path.read_text(encoding="utf-8")


class TestMetricsKSampleAndTrials:
    def test_trials_averages(self) -> None:
        r1 = ProblemResult(task_id="a", passed=True, trials_launched=3, trials_cancelled=2)
        r1._trials = [TrialRecord(index=0, rounds=[RoundRecord(round=1, passed=True)])]
        metrics = compute_metrics([r1])
        assert metrics["avg_trials_launched"] == 3.0
        assert metrics["avg_trials_cancelled"] == 2.0

    def test_result_json_roundtrip_has_no_private_fields(self, tmp_path: Path) -> None:
        from flowcoder.eval.report import write_report

        r = ProblemResult(task_id="a", passed=True, exit_code=0)
        r._trials = [TrialRecord(index=0, generated_code="x = 1")]
        _, json_path = write_report([r], compute_metrics([r]), {}, tmp_path)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert all(not k.startswith("_") for k in payload["results"][0])
