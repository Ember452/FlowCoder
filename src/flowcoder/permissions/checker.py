"""权限预检核心 PermissionChecker。

分层决策 allow / deny / ask。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowcoder.permissions.dangerous import DangerousCommandDetector, is_safe_command
from flowcoder.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from flowcoder.permissions.rules import (
    RuleEngine,
)
from flowcoder.permissions.sandbox import PathSandbox
from flowcoder.permissions.tool_fields import extract_content, extract_sandbox_path
from flowcoder.tools.base import Tool

_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion", "ExitPlanMode"})
# Plan 模式下无需沙箱/规则/模式判定的白名单工具——这些工具不触达文件系统且是规划流程所需。


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str


class PermissionChecker:
    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""

    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        """对一次工具调用按层做权限判定，返回 allow/deny/ask；靠前的层命中即短路返回。"""
        content = extract_content(tool.name, arguments)

        # Layer 0: Plan 模式例外放行
        decision = self._check_plan_mode(tool, content)
        if decision is not None:
            return decision

        decision = self._check_command_safety(tool, content)
        if decision is not None:
            return decision

        # Layer 2: 路径沙箱（仅实际触达文件系统的工具）
        decision = self._check_path_sandbox(tool, arguments)
        if decision is not None:
            return decision

        # Layer 3: 规则引擎匹配
        decision = self._check_rules(tool, content)
        if decision is not None:
            return decision

        # Layer 4: 权限模式兜底判定
        decision = self._check_mode_fallback(tool)
        if decision is not None:
            return decision

        # Layer 5: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")

    def _check_plan_mode(self, tool: Tool, content: str) -> Decision | None:
        """Plan 模式下白名单工具与规划文件写入直接放行，其余内容返回 None 交给下一层。"""
        if self.mode != PermissionMode.PLAN:
            return None
        if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
            return Decision(effect="allow", reason="Plan mode: allowed tool")
        if tool.name in ("WriteFile", "EditFile") and content:
            if self._is_plan_file(content):
                ok, reason = self.sandbox.check(content)
                if not ok:
                    return Decision(
                        effect="deny",
                        reason=f"路径沙箱拦截: {reason}",
                    )
                return Decision(effect="allow", reason="Plan mode: plan file write")
        return None

    def _check_command_safety(self, tool: Tool, content: str) -> Decision | None:
        """命令类工具先放行只读安全命令，再拦截危险命令；否则返回 None 交给下一层。"""
        if tool.category != "command":
            return None
        # Layer 1: 安全的只读命令（自动放行）
        if is_safe_command(content or ""):
            return Decision(effect="allow", reason="Safe read-only command")

        # Layer 1b: 危险命令黑名单（仅 Bash）
        hit, reason = self.detector.detect(content)
        if hit:
            return Decision(effect="deny", reason=f"危险命令拦截: {reason}")
        return None

    def _check_path_sandbox(
        self,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> Decision | None:
        """对读写类工具做路径沙箱校验，越界则判定 deny；不涉路径或校验通过返回 None。"""
        if tool.category not in ("read", "write"):
            return None
        target_path = extract_sandbox_path(tool.name, arguments)
        if target_path is None:
            return None
        if not target_path.strip():
            return Decision(effect="deny", reason="路径沙箱拦截: 缺少路径参数")
        ok, reason = self.sandbox.check(target_path)
        if not ok:
            return Decision(effect="deny", reason=f"路径沙箱拦截: {reason}")
        return None

    def _check_rules(self, tool: Tool, content: str) -> Decision | None:
        """交给规则引擎匹配 allow/deny；都未命中返回 None 交给模式兜底。"""
        rule_result = self.rule_engine.evaluate(tool.name, content)
        if rule_result == "allow":
            return Decision(effect="allow", reason="权限规则放行")
        if rule_result == "deny":
            return Decision(effect="deny", reason="权限规则拒绝")
        return None

    def _check_mode_fallback(self, tool: Tool) -> Decision | None:
        """前序各层都不生效时，用权限模式矩阵兜底；结果为 ask 时返回 None 触发人工确认。"""
        effect = mode_decide(self.mode, tool.category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")
        return None

    def _is_plan_file(self, target_path: str) -> bool:
        """判断目标路径是否落在已登记的规划文件或统一规划目录下（正规化后比较）。"""
        if not target_path:
            return False

        target = self._normalize_plan_candidate(target_path)
        if target is None:
            return False

        if self.plan_file_path:
            plan = self._normalize_plan_candidate(self.plan_file_path)
            if plan is not None and target == plan:
                return True

        plans_dir = (self.sandbox.project_root / ".flowcoder" / "plans").resolve(strict=False)
        try:
            target.relative_to(plans_dir)
            return True
        except ValueError:
            return False

    def _normalize_plan_candidate(self, path: str) -> Path | None:
        """把给定路径正规化为绝对路径，供后续沙箱范围判断；解析失败返回 None。"""
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.sandbox.project_root / candidate
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
