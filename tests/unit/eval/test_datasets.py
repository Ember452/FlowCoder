"""数据集加载器测试（JSONL / gz / limit / 内置玩具题）。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from flowcoder.eval.datasets import (
    BUILTIN_TOY_PROBLEMS,
    HUMANEVAL_PLUS_SHA256,
    Problem,
    is_harness_compatible,
    load_problems,
    sha256_of,
)


def _write_jsonl(path: Path, n: int = 3, *, gz: bool = False) -> Path:
    lines = [
        json.dumps(
            {
                "task_id": f"HumanEval/{i}",
                "prompt": f"def f{i}(x) -> int:\n",
                "test": f"def check(candidate):\n    assert candidate(0) == {i}\n",
                "entry_point": f"f{i}",
                "canonical_solution": f"    return {i}",
            }
        )
        for i in range(n)
    ]
    payload = ("\n".join(lines) + "\n").encode()
    target = path.with_suffix(".jsonl.gz") if gz else path
    if gz:
        target.write_bytes(gzip.compress(payload))
    else:
        target.write_bytes(payload)
    return target


class TestLoadProblems:
    def test_load_jsonl(self, tmp_path: Path) -> None:
        path = _write_jsonl(tmp_path / "ds.jsonl")
        problems = load_problems(path)
        assert len(problems) == 3
        assert problems[0].task_id == "HumanEval/0"
        assert problems[0].entry_point == "f0"
        assert problems[2].canonical_solution == "    return 2"

    def test_limit_takes_first_n(self, tmp_path: Path) -> None:
        path = _write_jsonl(tmp_path / "ds.jsonl", n=10)
        problems = load_problems(path, limit=4)
        assert [p.task_id for p in problems] == [f"HumanEval/{i}" for i in range(4)]

    def test_gz_supported(self, tmp_path: Path) -> None:
        path = _write_jsonl(tmp_path / "ds.jsonl", gz=True)
        problems = load_problems(path)
        assert len(problems) == 3

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="download_humaneval_plus"):
            load_problems(tmp_path / "nope.jsonl")

    def test_sha256_helper(self, tmp_path: Path) -> None:
        path = tmp_path / "x.txt"
        path.write_bytes(b"hello")
        import hashlib

        assert sha256_of(path) == hashlib.sha256(b"hello").hexdigest()

    def test_expected_checksum_constant_shape(self) -> None:
        # 下载脚本与文档引用的校验和常量保持 64 位十六进制形态
        assert len(HUMANEVAL_PLUS_SHA256) == 64
        int(HUMANEVAL_PLUS_SHA256, 16)


class TestBuiltinToyProblems:
    def test_two_toy_problems(self) -> None:
        assert len(BUILTIN_TOY_PROBLEMS) == 2
        assert all(isinstance(p, Problem) for p in BUILTIN_TOY_PROBLEMS)

    def test_toy_snippets_define_check_only(self) -> None:
        # 约定：test 片段只定义 check(candidate)，入口调用由 harness 统一追加
        for p in BUILTIN_TOY_PROBLEMS:
            assert "def check(candidate)" in p.test
            assert f"check({p.entry_point})" not in p.test


class TestHarnessCompatibility:
    def test_standard_shape_compatible(self) -> None:
        p = Problem(
            task_id="HumanEval/0",
            prompt="def f(x):\n",
            test="def check(candidate):\n    assertion(candidate(*inp), exp, 0)\n",
            entry_point="f",
        )
        assert is_harness_compatible(p)

    def test_special_oracle_incompatible(self) -> None:
        # HumanEval/32 形状：返回值解包变换，轻量 harness 会误判
        p = Problem(
            task_id="HumanEval/32",
            prompt="def find_zero(xs):\n",
            test="def check(candidate):\n    assert _poly(*candidate(*inp), inp) <= 0.0001\n",
            entry_point="find_zero",
        )
        assert not is_harness_compatible(p)
