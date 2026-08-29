"""评测指标：pass@1、平均 token 成本、平均耗时。"""

from __future__ import annotations

from flowcoder.eval.runner import ProblemResult


def compute_metrics(results: list[ProblemResult]) -> dict[str, float | int]:
    """从逐题结果聚合指标。

    skipped（special-oracle 跳过）不计入 pass@1 分母；生成错误与超时
    在已评测题中都计为未通过。
    """
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "skipped": 0,
            "evaluated": 0,
            "passed": 0,
            "pass_at_1": 0.0,
            "gen_errors": 0,
            "timeouts": 0,
            "avg_input_tokens": 0.0,
            "avg_output_tokens": 0.0,
            "avg_duration_ms": 0.0,
        }
    skipped = sum(1 for r in results if r.skipped)
    evaluated = total - skipped
    passed = sum(1 for r in results if r.passed)
    gen_errors = sum(1 for r in results if r.gen_error is not None and not r.skipped)
    timeouts = sum(1 for r in results if r.timed_out)
    tokens_in = [r.input_tokens for r in results if not r.skipped]
    tokens_out = [r.output_tokens for r in results if not r.skipped]
    durations = [r.duration_ms for r in results if not r.skipped]
    return {
        "total": total,
        "skipped": skipped,
        "evaluated": evaluated,
        "passed": passed,
        "pass_at_1": passed / evaluated if evaluated else 0.0,
        "gen_errors": gen_errors,
        "timeouts": timeouts,
        "avg_input_tokens": sum(tokens_in) / len(tokens_in) if tokens_in else 0.0,
        "avg_output_tokens": sum(tokens_out) / len(tokens_out) if tokens_out else 0.0,
        "avg_duration_ms": sum(durations) / len(durations) if durations else 0.0,
    }
