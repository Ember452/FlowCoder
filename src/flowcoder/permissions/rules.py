"""权限规则引擎。

解析 allow/deny 规则文件并匹配工具调用内容。"""

from __future__ import annotations

from flowcoder.permissions.tool_fields import extract_sandbox_path as extract_sandbox_path

from flowcoder.permissions.tool_fields import extract_content as extract_content

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import yaml


Effect = Literal["allow", "deny"]

# 规则语法：ToolName(glob 模式)，如 Bash(rm -rf *)
_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")


@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect

    def matches(self, tool_name: str, content: str) -> bool:
        # 工具名必须精确匹配，内容用 shell 通配符（fnmatch）匹配模式
        if self.tool_name != tool_name:
            return False
        return fnmatch(content, self.pattern)


def parse_rule(raw: str, effect: Effect) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    return Rule(tool_name=m.group(1), pattern=m.group(2), effect=effect)


def _load_rules_file(path: Path) -> list[Rule]:
    # 规则文件是 YAML 列表，每项 {rule: "Tool(pattern)", effect: allow|deny}
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if not isinstance(rule_str, str):
            continue
        if effect not in ("allow", "deny"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules


class RuleEngine:
    """分层 allow/deny 规则引擎。

    规则来自三个文件，构成三层优先级（按加载顺序）：
    user（用户全局）→ project（项目共享）→ local（项目本地、运行时可写）。
    匹配时同层内"后写的规则优先"（reversed），跨层则后加载的层整体更优先。
    """

    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path

    def _load_tiers(self) -> list[list[Rule]]:
        # 三层顺序固定：user → project → local（local 整体优先级最高）
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers

    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        # 逐层匹配，命中即返回；同层内 reversed 使后写规则覆盖先写规则。
        # 三层都未命中则返回 None，交由上层 PermissionChecker 的模式兜底判定。
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return None

    def append_local_rule(self, rule: Rule) -> None:
        # 运行时学习：用户在交互中"总是允许/拒绝"某操作时，追加到 local 规则文件
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        existing.append(rule)
        entries = [{"rule": f"{r.tool_name}({r.pattern})", "effect": r.effect} for r in existing]
        self._local_path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")
