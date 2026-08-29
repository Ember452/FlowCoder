"""下载 HumanEval+ 数据集（EvalPlus 格式）并做 sha256 校验。

用法：
    python scripts/download_humaneval_plus.py                # 默认 eval-data/
    python scripts/download_humaneval_plus.py --out 其他路径

数据文件不入 git（eval-data/ 已在 .gitignore）。数据来源：
    evalplus/humanevalplus 仓库的 test.jsonl（164 题，HumanEval+ 完整版）
    主源：  Hugging Face（evalplus/humanevalplus）
    备源：  hf-mirror.com（HF 镜像）
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

EXPECTED_SHA256 = "908377f1daf28dcb36846db73a5662b2e05a9907407c2696c89ad9d3b0b04492"
EXPECTED_SIZE = 11_452_868

SOURCES = [
    "https://huggingface.co/datasets/evalplus/humanevalplus/resolve/main/test.jsonl",
    "https://hf-mirror.com/datasets/evalplus/humanevalplus/resolve/main/test.jsonl",
]


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(out: Path) -> int:
    data: bytes | None = None
    for url in SOURCES:
        print(f"尝试下载: {url}")
        try:
            with urllib.request.urlopen(url, timeout=300) as resp:  # noqa: S310
                data = resp.read()
            break
        except Exception as e:  # 逐个备源尝试，全部失败才退出
            print(f"  失败: {e}", file=sys.stderr)
    if data is None:
        print("全部下载源失败；可手动下载数据文件后放到目标路径", file=sys.stderr)
        return 1

    actual = sha256_of(data)
    if actual != EXPECTED_SHA256:
        print(
            f"校验失败！期望 sha256={EXPECTED_SHA256}，实际 sha256={actual}，"
            "已拒绝写入。请检查下载源是否被篡改。",
            file=sys.stderr,
        )
        return 1
    if len(data) != EXPECTED_SIZE:
        print(
            f"字节数异常：期望 {EXPECTED_SIZE}，实际 {len(data)}（sha256 已通过，继续写入）",
            file=sys.stderr,
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"已写入 {out}（{len(data)} 字节，sha256 校验通过）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(Path("eval-data") / "humaneval_plus.jsonl"),
        help="输出路径（默认 eval-data/humaneval_plus.jsonl）",
    )
    return download(Path(parser.parse_args().out))


if __name__ == "__main__":
    sys.exit(main())
