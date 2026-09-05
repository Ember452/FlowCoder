"""危险命令检测与安全只读命令识别。"""

from __future__ import annotations

import re
import shlex

_RM_ROOT_REASON = "递归强制删除根目录"

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/\s*$"), _RM_ROOT_REASON),
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"curl\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
]


_SAFE_EXACT_COMMANDS = frozenset(
    {
        "pwd",
        "whoami",
        "hostname",
        "date",
        "cal",
        "uptime",
        "true",
        "false",
        "go version",
        "node -v",
        "npm -v",
        "python --version",
        "cargo --version",
        "rustc --version",
        "java -version",
        "java --version",
    }
)

_SAFE_PREFIX_COMMANDS = frozenset(
    {
        "ls",
        "dir",
        "echo",
        "cat",
        "head",
        "tail",
        "wc",
        "which",
        "whereis",
        "uname",
        "df",
        "du",
        "free",
        "file",
        "stat",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "uniq",
        "tr",
        "cut",
        "grep",
        "egrep",
        "fgrep",
        "diff",
        "comm",
        "test",
        "git status",
        "git log",
        "git diff",
        "git show",
        "git branch",
        "git tag",
        "git remote",
        "git rev-parse",
        "git ls-files",
        "git blame",
        "git stash list",
        "pip list",
    }
)


# 这些命令会输出文件内容，参数指向凭据类路径时不能免审批放行：
# 否则 Agent 可无审批读取 API key/JWT 等密钥，经对话上下文送给 LLM 供应商。
_FILE_CONTENT_COMMANDS = frozenset(
    {"cat", "head", "tail", "grep", "egrep", "fgrep", "diff", "comm"}
)

_SENSITIVE_PATH_MARKERS = (
    ".flowcoder",
    ".ssh",
    ".aws",
    ".kube",
    ".gnupg",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    ".env",
)


def _references_sensitive_path(command: str) -> bool:
    """判断命令参数是否指向凭据/密钥类路径（大小写不敏感，兼容 POSIX 与 Windows 分隔符）。"""
    lowered = command.lower()
    return any(marker in lowered for marker in _SENSITIVE_PATH_MARKERS)


def _tokenize_command(command: str) -> list[str]:
    """把命令按 shell 词法切分；切分失败时退化为按空白切分，避免误判。"""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _short_rm_flags(flag: str) -> tuple[bool, bool]:
    """提取短参数中（如 -rf）是否含递归 r 与强制 f 标记；长参数或非选项返回两个 False。"""
    if not flag.startswith("-") or flag.startswith("--"):
        return False, False
    chars = flag[1:].lower()
    return "r" in chars, "f" in chars


def _detect_rm_root(command: str) -> bool:
    """识别递归强制删除根目录（rm -rf /）的命令；只在递归且强制的组合下才判定为危险。"""
    tokens = _tokenize_command(command)
    for index, token in enumerate(tokens):
        if token != "rm":
            continue

        recursive = False
        force = False
        for arg in tokens[index + 1 :]:
            if arg in {";", "&&", "||", "|"}:
                break
            short_recursive, short_force = _short_rm_flags(arg)
            if arg == "--recursive" or short_recursive:
                recursive = True
            if arg == "--force" or short_force:
                force = True
            if (
                arg in {"--recursive", "--force", "--no-preserve-root"}
                or short_recursive
                or short_force
            ):
                continue
            if arg in {"/", "/*"}:
                return recursive and force
            if not arg.startswith("-"):
                break
    return False


def is_safe_command(command: str) -> bool:
    """判断命令是否为无需审批的只读安全命令：不含管道/重定向等复合符，且在安全白名单内。"""
    trimmed = command.strip()
    if not trimmed:
        return False
    for ch in ("|", ";", "&&", "||", ">", "<", "`", "\n", "\r"):
        if ch in trimmed:
            return False
    # 命令经 shell 执行，含 $ 会在运行期展开变量（如 echo $API_KEY），
    # 白名单命令都是无需变量的只读操作，含 $ 一律转人工审批。
    if "$" in trimmed:
        return False
    if trimmed in _SAFE_EXACT_COMMANDS:
        return True
    for safe in _SAFE_PREFIX_COMMANDS:
        if trimmed == safe or trimmed.startswith(safe + " "):
            if safe in _FILE_CONTENT_COMMANDS and _references_sensitive_path(trimmed):
                return False
            return True
    return False


class DangerousCommandDetector:
    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None) -> None:
        self._patterns = list(_DANGEROUS_PATTERNS)
        if extra_patterns:
            for regex_str, reason in extra_patterns:
                self._patterns.append((re.compile(regex_str), reason))

    def detect(self, command: str) -> tuple[bool, str]:
        """对命令做 rm 根目录专项检查与正则黑名单匹配，命中返回 (True, 原因)。"""
        if _detect_rm_root(command):
            return True, _RM_ROOT_REASON
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""
