"""评测指标：pass@1、平均 token 成本、平均耗时、自愈回收率、失败分布。"""

from __future__ import annotations

from collections import Counter

from flowcoder.eval.failure_tax import FAIL_CATEGORIES
from flowcoder.eval.runner import ProblemResult


def compute_metrics(results: list[ProblemResult]) -> dict[str, float | int]:
    """从逐题结果聚合指标。

    skipped（special-oracle 跳过）不计入 pass@1 分母；生成错误与超时
    在已评测题中都计为未通过。自愈回收率 = 首轮失败但最终通过的题数
    / 首轮失败的题数（含最终仍未通过的）。
    """
    total = len(results)
    if total == 0:
        base: dict[str, float | int] = {
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
        base.update(_empty_heal_and_failure())
        return base

    skipped = sum(1 for r in results if r.skipped)
    evaluated = total - skipped
    passed = sum(1 for r in results if r.passed)
    gen_errors = sum(1 for r in results if r.gen_error is not None and not r.skipped)
    timeouts = sum(1 for r in results if r.timed_out)
    tokens_in = [r.input_tokens for r in results if not r.skipped]
    tokens_out = [r.output_tokens for r in results if not r.skipped]
    durations = [r.duration_ms for r in results if not r.skipped]
    metrics: dict[str, float | int] = {
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
    metrics.update(_heal_and_failure_metrics(results))
    return metrics


def _empty_heal_and_failure() -> dict[str, float | int]:
    out: dict[str, float | int] = {
        "first_round_failures": 0,
        "healed": 0,
        "heal_recovery_rate": 0.0,
        "avg_trials_launched": 0.0,
        "avg_trials_cancelled": 0.0,
    }
    for cat in FAIL_CATEGORIES:
        out[f"fail_{cat}"] = 0
    return out


def _heal_and_failure_metrics(results: list[ProblemResult]) -> dict[str, float | int]:
    evaluated = [r for r in results if not r.skipped]
    first_round_failures = 0
    healed = 0
    for r in evaluated:
        trials = r.trials
        if not trials:
            continue
        final = next((t for t in trials if t.passed), trials[-1])
        if final.rounds and not final.rounds[0].passed:
            first_round_failures += 1
            if r.passed:
                healed += 1
    distribution = Counter(
        r.failure_category for r in evaluated if not r.passed and r.failure_category
    )
    out: dict[str, float | int] = {
        "first_round_failures": first_round_failures,
        "healed": healed,
        "heal_recovery_rate": healed / first_round_failures if first_round_failures else 0.0,
        "avg_trials_launched": (
            sum(r.trials_launched for r in evaluated) / len(evaluated) if evaluated else 0.0
        ),
        "avg_trials_cancelled": (
            sum(r.trials_cancelled for r in evaluated) / len(evaluated) if evaluated else 0.0
        ),
    }
    for cat in FAIL_CATEGORIES:
        out[f"fail_{cat}"] = distribution.get(cat, 0)
    return out
