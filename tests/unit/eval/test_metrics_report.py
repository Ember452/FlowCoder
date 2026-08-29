"""指标聚合与报告产出测试。"""

from __future__ import annotations

import json
from pathlib import Path

from flowcoder.eval.metrics import compute_metrics
from flowcoder.eval.report import report_filename, write_report
from flowcoder.eval.runner import ProblemResult


def _result(
    task_id: str, *, passed: bool = True, duration_ms: int = 100, tokens: int = 50
) -> ProblemResult:
    return ProblemResult(
        task_id=task_id,
        passed=passed,
        exit_code=0 if passed else 1,
        duration_ms=duration_ms,
        input_tokens=tokens,
        output_tokens=tokens,
    )


class TestComputeMetrics:
    def test_all_pass(self) -> None:
        metrics = compute_metrics([_result("a"), _result("b")])
        assert metrics["total"] == 2
        assert metrics["passed"] == 2
        assert metrics["pass_at_1"] == 1.0
        assert metrics["avg_duration_ms"] == 100.0
        assert metrics["avg_input_tokens"] == 50.0

    def test_mixed_counts_gen_error_and_timeout_as_fail(self) -> None:
        fail = _result("f", passed=False)
        timeout = _result("t", passed=False)
        timeout.timed_out = True
        gen_err = ProblemResult(task_id="g", gen_error="LLM 429")
        metrics = compute_metrics([_result("ok"), fail, timeout, gen_err])
        assert metrics["total"] == 4
        assert metrics["passed"] == 1
        assert metrics["pass_at_1"] == 0.25
        assert metrics["timeouts"] == 1
        assert metrics["gen_errors"] == 1

    def test_skipped_excluded_from_denominator(self) -> None:
        ok = _result("ok")
        skipped = ProblemResult(task_id="s", skipped=True)
        metrics = compute_metrics([ok, skipped])
        assert metrics["total"] == 2
        assert metrics["skipped"] == 1
        assert metrics["evaluated"] == 1
        assert metrics["pass_at_1"] == 1.0  # 分母只含已评测题

    def test_all_skipped(self) -> None:
        metrics = compute_metrics([ProblemResult(task_id="s", skipped=True)])
        assert metrics["evaluated"] == 0
        assert metrics["pass_at_1"] == 0.0
        assert metrics["avg_input_tokens"] == 0.0

    def test_empty(self) -> None:
        metrics = compute_metrics([])
        assert metrics["total"] == 0
        assert metrics["pass_at_1"] == 0.0


class TestReport:
    def test_writes_markdown_and_json(self, tmp_path: Path) -> None:
        results = [_result("a"), _result("b", passed=False)]
        metrics = compute_metrics(results)
        meta = {"dataset": "eval-data/humaneval_plus.jsonl", "limit": "50"}
        md_path, json_path = write_report(results, metrics, meta, tmp_path)

        assert md_path.exists() and json_path.exists()
        assert md_path.name.startswith("report-") and md_path.suffix == ".md"
        assert json_path.stem == md_path.stem

        md = md_path.read_text(encoding="utf-8")
        assert "# HumanEval+ 评测报告" in md
        assert "| a | ✅" in md
        assert "| b | ❌" in md
        assert "dataset: eval-data/humaneval_plus.jsonl" in md

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["meta"] == meta
        assert payload["metrics"]["pass_at_1"] == 0.5
        assert len(payload["results"]) == 2
        # 内部字段（下划线开头）不得写入报告
        assert all(not k.startswith("_") for r in payload["results"] for k in r)

    def test_report_filename_timestamped(self) -> None:
        from datetime import datetime

        name = report_filename("report", when=datetime(2026, 8, 29, 12, 0, 0))
        assert name == "report-20260829-120000"
