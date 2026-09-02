"""项目指令加载（含 include 展开）。"""

from __future__ import annotations

from pathlib import Path

MAX_INCLUDE_DEPTH = 5
INCLUDE_PREFIX = "@include "


def process_includes(
    content: str,
    base_dir: Path,
    project_root: Path,
    depth: int = 0,
) -> str:
    """递归展开 @include 指令。

    对 content 中每行 ``@include <相对路径>`` 读取目标文件并递归处理；
    超出最大深度、路径越出项目根或文件不存在时，原样保留一条失败占位注释，
    不中断剩余内容。返回展开后的完整文本。
    """
    if depth >= MAX_INCLUDE_DEPTH:
        return content

    resolved_root = project_root.resolve()
    lines = content.split("\n")
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(INCLUDE_PREFIX):
            result.append(line)
            continue

        rel_path = stripped[len(INCLUDE_PREFIX) :].strip()
        abs_path = (base_dir / rel_path).resolve()

        try:
            abs_path.relative_to(resolved_root)
        except ValueError:
            result.append("<!-- @include blocked: path outside project -->")
            continue

        if not abs_path.exists() or not abs_path.is_file():
            result.append("<!-- @include skipped: file not found -->")
            continue

        included = abs_path.read_text(encoding="utf-8")
        processed = process_includes(included, abs_path.parent, project_root, depth + 1)
        result.append(processed)

    return "\n".join(result)


def load_instructions(project_root: str) -> str:
    """按优先级加载项目/用户指令：项目根 FLOWCODER.md → .flowcoder/FLOWCODER.md → 用户级。

    各自先做 @include 展开，再用 ``\n---\n`` 分隔拼接返回（哪个文件有力覆盖前者）。
    """
    root = Path(project_root)
    home = Path.home()

    paths = [
        root / "FLOWCODER.md",
        root / ".flowcoder" / "FLOWCODER.md",
        home / ".flowcoder" / "FLOWCODER.md",
    ]

    sections: list[str] = []
    for path in paths:
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8")
            processed = process_includes(content, path.parent, root)
            sections.append(processed)

    return "\n---\n".join(sections)
