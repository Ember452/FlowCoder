"""评测 CLI：python -m flowcoder.eval --dataset <path> --limit 50。

真实 LLM 路径：加载项目配置创建 Agent，配合容器池沙箱执行测试。
权限模式固定 bypassPermissions（评测是无人的批量消费场景，且评测提示
不要求工具调用；见 docs/specs P2a ADR）。
温度默认固定 0.0（可复现性验收），--temperature 覆盖。

--compare 跑对比矩阵（P2b 验收）：
  k=1,heal=0（无自愈） / k=1,heal=3（有自愈） / k=3,heal=0（k-sample 首胜）
产出 comparison-<时间戳>.md/.json。
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
    parser.add_argument("--heal-rounds", type=int, default=3, help="自愈修复轮上限（0=关闭）")
    parser.add_argument("--k", type=int, default=3, help="每题并行 trial 数（k-sample 首胜）")
    parser.add_argument(
        "--temperature", type=float, default=0.0, help="采样温度（默认 0.0，数字可复现）"
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="跑对比矩阵：无自愈/有自愈 × k=1/k=3，产出对比报告",
    )
    return parser


def _base_meta(args: argparse.Namespace) -> dict[str, str]:
    return {
        "dataset": str(args.dataset),
        "limit": str(args.limit),
        "concurrency": str(args.concurrency),
        "timeout_s": str(args.timeout),
        "sandbox_image": args.image,
        "temperature": str(args.temperature),
    }


async def _make_solver(args: argparse.Namespace):
    """按温度覆盖后的配置构建会话式 Solver（每会话独立 Agent 实例）。"""
    from flowcoder.agent.factory import create_agent_from_config
    from flowcoder.eval import LiveAgentSolver

    config = load_config()
    provider = config.providers[0]
    provider.temperature = args.temperature
    _agent, _deps = await create_agent_from_config(
        config, work_dir=str(Path.cwd()), permission_mode=PermissionMode.BYPASS
    )
    # k-sample 并行 trial 不能共享 Agent 实例（内部有可变状态）：
    # 工厂按会话建新 Agent，复用已解析好的 client / registry / 权限检查器
    base = _agent

    def agent_factory():
        from flowcoder.agent import Agent

        return Agent(
            client=base.client,
            registry=base.registry,
            protocol=base.protocol,
            work_dir=base.work_dir,
            permission_checker=base.permission_checker,
            context_window=base.context_window,
            instructions_content=base.instructions_content,
            memory_hub=None,
            hook_engine=None,
        )

    return LiveAgentSolver(agent_factory)


async def _run_once(args: argparse.Namespace, problems, label: str):
    """构建执行器与 Runner 运行一轮，写报告并返回 (results, metrics)。"""
    from flowcoder.eval import (
        DockerSandboxExecutor,
        EvalRunner,
        compute_metrics,
        write_report,
    )

    solver = await _make_solver(args)
    executor = DockerSandboxExecutor(image=args.image, pool_size=args.pool_size)
    runner = EvalRunner(
        solver,
        executor,
        concurrency=args.concurrency,
        timeout_s=args.timeout,
        heal_rounds=args.heal_rounds,
        k=args.k,
    )
    print(f"[{label}] 开始评测 {len(problems)} 道题...")
    results = await runner.run(problems)
    metrics = compute_metrics(results)
    meta = _base_meta(args)
    meta.update({"heal_rounds": str(args.heal_rounds), "k": str(args.k), "run": label})
    md_path, json_path = write_report(results, metrics, meta, args.output_dir)
    print(f"[{label}] pass@1={metrics['pass_at_1']} 报告: {md_path}")
    return results, metrics


async def _run_compare(args: argparse.Namespace, problems) -> int:
    """跑对比矩阵（无自愈/有自愈 × k=1/k=3），产出一份对比报告。"""
    from flowcoder.eval import write_comparison_report

    matrix = [
        ("k=1,heal=0", 1, 0),
        ("k=1,heal=3", 1, 3),
        ("k=3,heal=0", 3, 0),
    ]
    runs: dict[str, dict] = {}
    results_by_run: dict[str, list] = {}
    for label, k, heal in matrix:
        args.k, args.heal_rounds = k, heal
        results, metrics = await _run_once(args, problems, label)
        runs[label] = metrics
        results_by_run[label] = results

    meta = _base_meta(args)
    meta["matrix"] = "; ".join(f"{lbl}: k={k},heal={h}" for lbl, k, h in matrix)
    md_path, json_path = write_comparison_report(runs, results_by_run, meta, args.output_dir)
    print(f"\n对比报告: {md_path}\n          {json_path}")
    return 0


async def run_eval(args: argparse.Namespace) -> int:
    """入口：加载数据集并分派到单轮或对比评测，返回进程退出码。"""
    from flowcoder.eval import load_problems

    problems = load_problems(args.dataset, limit=args.limit)
    if not problems:
        print("数据集为空", file=sys.stderr)
        return 1
    print(
        f"加载 {len(problems)} 道题，并发 {args.concurrency}，"
        f"每题执行超时 {args.timeout}s，k={args.k}，自愈轮 {args.heal_rounds}"
    )

    if args.compare:
        return await _run_compare(args, problems)

    results, metrics = await _run_once(args, problems, "single")
    print("\n=== 指标汇总 ===")
    for k, v in metrics.items():
        print(f"{k}: {v}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(asyncio.run(run_eval(args)))


if __name__ == "__main__":
    main()
