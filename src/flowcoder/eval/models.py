"""评测数据模型与协议定义（P5 重构拆分，行为零变化）。

Runner（编排）、Solver（生成）、Executor（执行）三类变化理由各自独立；
本模块只放稳定的数据结构与协议，见 docs/architecture/eval.md。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from flowcoder.eval.datasets import Problem

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
