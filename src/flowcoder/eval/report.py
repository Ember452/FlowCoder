"""评测报告：产出 Markdown + JSON 到 eval-results/（目录不入 git）。"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flowcoder.eval.runner import ProblemResult

#: 默认输出目录（.gitignore 已登记）
DEFAULT_OUTPUT_DIR = Path("eval-results")


def report_filename(stem: str, when: datetime | None = None) -> str:
    ts = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stem}-{ts}"


def _markdown_table(results: list[ProblemResult]) -> str:
    lines = [
        "| task_id | passed | exit_code | timed_out | duration_ms | in_tokens | out_tokens |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.task_id} | {'✅' if r.passed else '❌'} | {r.exit_code} "
            f"| {r.timed_out} | {r.duration_ms} | {r.input_tokens} | {r.output_tokens} |"
        )
    return "\n".join(lines)


def write_report(
    results: list[ProblemResult],
    metrics: dict[str, float | int],
    meta: dict[str, str],
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    """写 report-<时间戳>.md 与 .json，返回两个文件路径。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = report_filename("report")
    md_path = out / f"{stem}.md"
    json_path = out / f"{stem}.json"

    meta_lines = "\n".join(f"- {k}: {v}" for k, v in meta.items())
    summary_lines = "\n".join(f"- {k}: {v}" for k, v in metrics.items())
    md = (
        f"# HumanEval+ 评测报告\n\n"
        f"## 运行配置\n\n{meta_lines}\n\n"
        f"## 指标汇总\n\n{summary_lines}\n\n"
        f"## 逐题结果\n\n{_markdown_table(results)}\n"
    )
    md_path.write_text(md, encoding="utf-8")

    payload = {
        "meta": meta,
        "metrics": metrics,
        "results": [{k: v for k, v in asdict(r).items() if not k.startswith("_")} for r in results],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


_COMPARISON_COLUMNS = (
    ("pass_at_1", "pass@1"),
    ("evaluated", "evaluated"),
    ("passed", "passed"),
    ("healed", "healed"),
    ("heal_recovery_rate", "自愈回收率"),
    ("avg_input_tokens", "avg in tokens"),
    ("avg_output_tokens", "avg out tokens"),
    ("avg_duration_ms", "avg exec ms"),
    ("fail_编译错", "编译错"),
    ("fail_逻辑错", "逻辑错"),
    ("fail_测试理解错", "测试理解错"),
    ("fail_超预算", "超预算"),
    ("avg_trials_cancelled", "avg cancelled"),
)


def write_comparison_report(
    runs: dict[str, dict[str, float | int]],
    results_by_run: dict[str, list[ProblemResult]],
    meta: dict[str, str],
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    """对比多组运行（如 无自愈/有自愈、k=1/k=3），产出 comparison-<ts>.md/.json。

    runs: 运行标签 → 指标；results_by_run: 运行标签 → 逐题结果（用于逐题矩阵）。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = report_filename("comparison")
    md_path = out / f"{stem}.md"
    json_path = out / f"{stem}.json"

    labels = list(runs)
    header = "| 指标 | " + " | ".join(labels) + " |"
    sep = "|---|" + "---|" * len(labels)
    rows = [header, sep]
    for key, title in _COMPARISON_COLUMNS:
        cells = []
        for label in labels:
            value = runs[label].get(key, "-")
            if isinstance(value, float):
                value = f"{value:.4f}"
            cells.append(str(value))
        rows.append(f"| {title} | " + " | ".join(cells) + " |")

    # 逐题通过矩阵
    all_ids: list[str] = []
    seen: set[str] = set()
    for label in labels:
        for r in results_by_run.get(label, []):
            if r.task_id not in seen:
                seen.add(r.task_id)
                all_ids.append(r.task_id)
    matrix = ["", "## 逐题通过矩阵", "", "| task_id | " + " | ".join(labels) + " |", sep]
    for task_id in all_ids:
        cells = []
        for label in labels:
            r = next((x for x in results_by_run.get(label, []) if x.task_id == task_id), None)
            if r is None:
                cells.append("-")
            elif r.skipped:
                cells.append("⏭️")
            else:
                cells.append("✅" if r.passed else "❌")
        matrix.append(f"| {task_id} | " + " | ".join(cells) + " |")

    meta_lines = "\n".join(f"- {k}: {v}" for k, v in meta.items())
    md = (
        "# HumanEval+ 对比评测报告\n\n"
        f"## 运行配置\n\n{meta_lines}\n\n"
        f"## 对比总表\n\n" + "\n".join(rows) + "\n" + "\n".join(matrix) + "\n"
    )
    md_path.write_text(md, encoding="utf-8")

    payload = {
        "meta": meta,
        "runs": runs,
        "results": {
            label: [
                {k: v for k, v in asdict(r).items() if not k.startswith("_")}
                for r in results_by_run.get(label, [])
            ]
            for label in labels
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
