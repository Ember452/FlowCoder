"""评测 CLI：python -m flowcoder.eval --dataset <path> --limit 50。

真实 LLM 路径：加载项目配置创建 Agent，配合容器池沙箱执行测试。
权限模式固定 bypassPermissions（评测是无人的批量消费场景，且评测提示
不要求工具调用；见 docs/specs P2a ADR）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from flowcoder.config import load_config
from flowcoder.permissions import PermissionMode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flowcoder.eval", description="HumanEval+ 评测流水线"
    )
    parser.add_argument(
        "--dataset",
        default=str(Path("eval-data") / "humaneval_plus.jsonl"),
        help="HumanEval+ JSONL 文件路径（默认 eval-data/humaneval_plus.jsonl）",
    )
    parser.add_argument("--limit", type=int, default=50, help="取前 N 道题（默认 50）")
    parser.add_argument("--concurrency", type=int, default=4, help="并发题数（默认 4）")
    parser.add_argument("--timeout", type=float, default=30.0, help="每题沙箱执行超时秒数")
    parser.add_argument("--output-dir", default="eval-results", help="报告输出目录")
    parser.add_argument("--image", default="python:3.11-slim", help="沙箱镜像")
    parser.add_argument("--pool-size", type=int, default=2, help="沙箱池规模")
    return parser


async def run_eval(args: argparse.Namespace) -> int:
    from flowcoder.agent.factory import create_agent_from_config
    from flowcoder.eval import (
        DockerSandboxExecutor,
        EvalRunner,
        LiveAgentSolver,
        compute_metrics,
        load_problems,
        write_report,
    )

    problems = load_problems(args.dataset, limit=args.limit)
    if not problems:
        print("数据集为空", file=sys.stderr)
        return 1
    print(f"加载 {len(problems)} 道题，并发 {args.concurrency}，每题执行超时 {args.timeout}s")

    config = load_config()
    agent, _deps = await create_agent_from_config(
        config, work_dir=str(Path.cwd()), permission_mode=PermissionMode.BYPASS
    )
    solver = LiveAgentSolver(agent)
    executor = DockerSandboxExecutor(image=args.image, pool_size=args.pool_size)
    runner = EvalRunner(solver, executor, concurrency=args.concurrency, timeout_s=args.timeout)

    results = await runner.run(problems)
    metrics = compute_metrics(results)
    meta = {
        "dataset": str(args.dataset),
        "limit": str(args.limit),
        "concurrency": str(args.concurrency),
        "timeout_s": str(args.timeout),
        "sandbox_image": args.image,
        "trials_per_problem": "1",
        "temperature": "provider-default（未显式固定，见 P2a ADR 限制一节）",
    }
    md_path, json_path = write_report(results, metrics, meta, args.output_dir)

    print("\n=== 指标汇总 ===")
    for k, v in metrics.items():
        print(f"{k}: {v}")
    print(f"\n报告: {md_path}\n      {json_path}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(asyncio.run(run_eval(args)))


if __name__ == "__main__":
    main()
