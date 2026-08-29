"""评测包：HumanEval+ 流水线（dataset / runner / metrics / failure_tax / report）。

评测是 Agent 的消费者（不改 agent/core.py）；执行复用 sandbox 模块，
单测经 SandboxExecutor / SolutionSolver Protocol 注入 fake，不依赖真实 Docker。
"""

from flowcoder.eval.datasets import (
    BUILTIN_TOY_PROBLEMS,
    DEFAULT_DATA_DIR,
    HUMANEVAL_PLUS_SHA256,
    Problem,
    is_harness_compatible,
    load_problems,
    sha256_of,
)
from flowcoder.eval.failure_tax import (
    FAIL_BUDGET,
    FAIL_COMPILE,
    FAIL_LOGIC,
    FAIL_CATEGORIES,
    FAIL_SPEC,
    classify_failure,
)
from flowcoder.eval.metrics import compute_metrics
from flowcoder.eval.report import DEFAULT_OUTPUT_DIR, write_comparison_report, write_report
from flowcoder.eval.runner import (
    AgentSolution,
    DockerSandboxExecutor,
    EvalRunner,
    ExecutionOutcome,
    LiveAgentSolver,
    ProblemResult,
    RoundRecord,
    SandboxExecutor,
    SolutionSolver,
    SolverSession,
    TrialRecord,
    build_test_harness,
    extract_code,
)

__all__ = [
    "BUILTIN_TOY_PROBLEMS",
    "DEFAULT_DATA_DIR",
    "DEFAULT_OUTPUT_DIR",
    "FAIL_BUDGET",
    "FAIL_COMPILE",
    "FAIL_LOGIC",
    "FAIL_CATEGORIES",
    "FAIL_SPEC",
    "HUMANEVAL_PLUS_SHA256",
    "AgentSolution",
    "DockerSandboxExecutor",
    "EvalRunner",
    "ExecutionOutcome",
    "LiveAgentSolver",
    "Problem",
    "ProblemResult",
    "RoundRecord",
    "SandboxExecutor",
    "SolutionSolver",
    "SolverSession",
    "TrialRecord",
    "build_test_harness",
    "classify_failure",
    "compute_metrics",
    "extract_code",
    "is_harness_compatible",
    "load_problems",
    "sha256_of",
    "write_comparison_report",
    "write_report",
]
