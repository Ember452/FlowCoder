## 变更说明

<!-- 一两句话说清这个 PR 做了什么、为什么 -->

## 变更类型

- [ ] 新功能（feat）
- [ ] 缺陷修复（fix）
- [ ] 重构（refactor，不改行为）
- [ ] 测试（test）
- [ ] 文档（docs）
- [ ] 其他（chore/perf）

## 自检清单

- [ ] `ruff check src tests` 与 `ruff format --check src tests` 通过
- [ ] `pytest tests/unit` 通过；涉及集成改动时 `pytest tests/integration` 通过
- [ ] 新功能附带测试；缺陷修复附带复现该缺陷的回归测试
- [ ] 改动符合 AGENTS.md 的依赖方向（下层不 import 上层）
- [ ] 未向 `app.py` 添加新逻辑
- [ ] 涉及架构决策的，已在 `docs/specs/` 留 ADR
- [ ] CHANGELOG.md 已更新（面向用户的变化）
