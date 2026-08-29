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
