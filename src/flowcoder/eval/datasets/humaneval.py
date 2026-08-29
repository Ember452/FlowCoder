"""HumanEval+ 数据集加载（EvalPlus 格式 JSONL）。"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

#: evalplus/humanevalplus 仓库 test.jsonl 的 sha256（HF LFS oid，2026-08-29 核对）
HUMANEVAL_PLUS_SHA256 = "908377f1daf28dcb36846db73a5662b2e05a9907407c2696c89ad9d3b0b04492"

#: 数据文件默认存放目录（git 忽略，不入库）
DEFAULT_DATA_DIR = Path("eval-data")


@dataclass(frozen=True)
class Problem:
    """一道补全式代码题（HumanEval+ 格式）。"""

    task_id: str
    prompt: str  # 函数签名 + docstring（含类型标注与示例）
    test: str  # 自带 helper 的测试片段，定义 check(candidate)
    entry_point: str  # 函数名
    canonical_solution: str = ""


def _parse_line(line: str) -> Problem:
    raw = json.loads(line)
    return Problem(
        task_id=raw["task_id"],
        prompt=raw["prompt"],
        test=raw["test"],
        entry_point=raw["entry_point"],
        canonical_solution=raw.get("canonical_solution", ""),
    )


def load_problems(path: str | Path, limit: int | None = None) -> list[Problem]:
    """从 JSONL（或 .jsonl.gz）加载题目，取前 limit 道。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"数据集文件不存在: {p}（先运行 scripts/download_humaneval_plus.py 下载）"
        )
    opener = gzip.open if p.suffix == ".gz" else open
    problems: list[Problem] = []
    with opener(p, "rt", encoding="utf-8") as f:  # type: ignore[operator]
        for line in f:
            line = line.strip()
            if not line:
                continue
            problems.append(_parse_line(line))
            if limit is not None and len(problems) >= limit:
                break
    return problems


def sha256_of(path: str | Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_harness_compatible(problem: Problem) -> bool:
    """判断该题能否被轻量 check() harness 正确判定。

    evalplus 官方 runner 不执行 test 片段的 check()，而是用预计算输入 +
    special oracle（如 HumanEval/32 的 ``assert _poly(*candidate(*inp), inp)``
    对返回值做变换后再比较）。这类题放进"解法 + test + check(entry_point)"
    的简单 harness 会对正确解法误报失败，必须跳过（识别特征：test 内含
    ``*candidate(`` 的返回值解包变换）。
    """
    return "*candidate(" not in problem.test


# ---------------------------------------------------------------------------
# 内置玩具题（单测用，结构与 HumanEval+ 完全一致，不依赖网络与第三方库）
# ---------------------------------------------------------------------------

_TOY_ADD_TEST = """def check(candidate):
    assert candidate(1, 2) == 3
    assert candidate(-1, 1) == 0
    assert candidate(0, 0) == 0
"""

_TOY_PALINDROME_TEST = """def check(candidate):
    assert candidate("") is True
    assert candidate("a") is True
    assert candidate("ab") is False
    assert candidate("aba") is True
"""

BUILTIN_TOY_PROBLEMS: tuple[Problem, ...] = (
    Problem(
        task_id="toy/add",
        prompt='def add(a: int, b: int) -> int:\n    """ 返回两个整数之和。"""\n',
        test=_TOY_ADD_TEST,
        entry_point="add",
        canonical_solution="    return a + b",
    ),
    Problem(
        task_id="toy/palindrome",
        prompt=('def is_palindrome(s: str) -> bool:\n    """ 判断字符串是否为回文。"""\n'),
        test=_TOY_PALINDROME_TEST,
        entry_point="is_palindrome",
        canonical_solution="    return s == s[::-1]",
    ),
)
