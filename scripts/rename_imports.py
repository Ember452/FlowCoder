"""批量替换 mozilcode → flowcoder 的全部形态。

扫描 src/、tests/、scripts/、pyproject.toml、顶层 *.md，
按"长串/大写优先"顺序应用规则，避免 MOZILCODE 被 mozilcode 规则误改。
不进 docs/，保留 commit-log.md / migration-plan.md 的迁移来源记录。
排除脚本自身（rename_imports.py 含 pattern 字符串字面量，不可改）。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "src",
    ROOT / "tests",
    ROOT / "scripts",
    ROOT / "pyproject.toml",
]
TARGETS += [p for p in ROOT.glob("*.md")]

# 顺序敏感：先 UPPER（含下划线连接的 UPPER 标识符），再 PascalCase，再 lower，再目录/文案
# 注意：\b 把 _ 当单词字符，故 MOZILCODE_XXX 和 mozilcode_xxx 都不会被 \b 规则匹配，必须用子串规则
PATTERNS = [
    (r"MOZILCODE_", "FLOWCODER_"),          # 所有 MOZILCODE_XXX env var / 常量（含 _MOZILCODE_THEME）
    (r"\bMOZILCODE\b", "FLOWCODER"),         # 独立 MOZILCODE（如有）
    (r"\bMozilCodeApp\b", "FlowCoderApp"),
    (r"\bMozilCode\b", "FlowCoder"),
    (r'"\.mozilcode"', '".flowcoder"'),
    (r"\.mozilcode/", ".flowcoder/"),
    (r"~/.mozilcode", "~/.flowcoder"),
    (r'prog="mozilcode"', 'prog="flowcoder"'),
    (r"\bmozilcode-daemon\b", "flowcoder-daemon"),
    (r"mozilcodeMd", "flowcoderMd"),         # markdown 标记 mozilcodeMd（\b 漏匹配）
    (r"mozilcode_", "flowcoder_"),           # 变量名/参数名/动态模块名（\b 漏匹配项）
    (r"\bmozilcode\b", "flowcoder"),
    # 迁移路径调整：移到 core/ 和 ui/ 子目录的模块的绝对 import 路径（幂等安全）
    (r"\bflowcoder\.cache\b", "flowcoder.core.cache"),
    (r"\bflowcoder\.serialization\b", "flowcoder.core.serialization"),
    (r"\bflowcoder\.frontmatter\b", "flowcoder.core.frontmatter"),
    (r"\bflowcoder\.driver\b", "flowcoder.core.driver"),
    (r"\bflowcoder\.askuser_dialog\b", "flowcoder.ui.askuser_dialog"),
    (r"\bflowcoder\.permission_dialog\b", "flowcoder.ui.permission_dialog"),
    (r"\bflowcoder\.plan_dialog\b", "flowcoder.ui.plan_dialog"),
    (r"\bflowcoder\.plan_paths\b", "flowcoder.ui.plan_paths"),
    (r"\bflowcoder\.session_dialog\b", "flowcoder.ui.session_dialog"),
    (r"\bflowcoder\.teammate_tree\b", "flowcoder.ui.teammate_tree"),
]


def walk():
    for root in TARGETS:
        if root.is_file():
            yield root
        else:
            yield from root.rglob("*")


changed = 0
for f in walk():
    if not f.is_file():
        continue
    if f.name == "rename_imports.py":
        continue  # 排除自身：含 pattern 字符串字面量
    if f.suffix not in {".py", ".toml", ".md", ".tcss"}:
        continue
    text = f.read_text(encoding="utf-8")
    new = text
    for pat, rep in PATTERNS:
        new = re.sub(pat, rep, new)
    if new != text:
        f.write_text(new, encoding="utf-8")
        changed += 1
        print(f"rewrote {f.relative_to(ROOT)}")
print(f"done, {changed} files changed")
