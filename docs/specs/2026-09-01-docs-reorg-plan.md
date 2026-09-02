# docs 体系重组执行计划（2026-09-01）

> 背景：根目录堆积 6 个工作文档、同类计划两个家、个人求职材料（简历写法 / 量化测试 /
> study）混在开源仓库、refactor-plan-akashic 泄露本机路径。经确认执行重组：
> **根目录只留门面（README / CHANGELOG / CONTRIBUTING / SECURITY / LICENSE / AGENTS.md /
> PROMPTS.md），计划归 docs/plans/，报告归 docs/reports/，个人材料归 docs/career/ 且不入 git。**

## 一、当前状态

**已完成**（staged，待最终核验后提交）：

| 变更 | 方式 | git 状态 |
|---|---|---|
| 根目录 4 个计划/审查 → `docs/plans/`（TRANSFORMATION / FRAMEWORK_REFACTOR / PROJECT_REVIEW / CODE_QUALITY_AUDIT） | git mv | R（保留历史） |
| `docs/chaos-report.md` → `docs/reports/` | git mv | R |
| `docs/refactor-plan-akashic.md` → `docs/plans/` | git mv | R |
| `docs/migration-plan.md` → `docs/plans/` | 普通 mv（本就未被 git 跟踪） | 无 |
| `DIFFERENTIATION_PLAN.md` → `docs/plans/` | 普通 mv（新文件未跟踪） | ?? |
| `docs/简历写法.md`、`docs/量化测试方法.md` → `docs/career/` 并解除跟踪 | git rm --cached + mv | D（staged） |
| `docs/study/` → `docs/career/study/` | 普通 mv（本就未被跟踪） | 无 |
| `docs/career/.gitignore`（`*` + `!.gitignore`） | 新建 | 待 git add |
| `.gitignore` 中 `docs/migration-plan.md` 路径同步 | sed | M |

**执行中发现的两件事**（重要）：

1. **`AGENTS.md` 本身在 `.gitignore` 里**（"IDE / 迁移过程文件（不进仓库）"一节）——即公开仓库
   看不到开发规范与文档路由表。是否取消 ignore 见"待决问题"。
2. sed 在 Git Bash 下因反斜杠转义故障（`Invalid back reference`）**未改动任何文件**，
  引用修复全部未做——这是剩余工作的主体。

## 二、剩余步骤（按序执行）

### 步骤 1：引用修复脚本

sed 在本环境不可靠，改用字节级替换的 Python 脚本（无编码 / 换行符副作用），
保存为 `scripts/fix_doc_refs.py`，跑完核验后删除：

```python
"""docs 重组引用修复（一次性脚本，核验后删除）。用法：python scripts/fix_doc_refs.py"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def pair(old: str, new: str) -> tuple[bytes, bytes]:
    return old.encode("utf-8"), new.encode("utf-8")

PLANS = ["TRANSFORMATION_PLAN.md", "FRAMEWORK_REFACTOR_PLAN.md",
         "PROJECT_REVIEW.md", "CODE_QUALITY_AUDIT.md", "DIFFERENTIATION_PLAN.md"]
PLAN_FIX = [pair(n, f"docs/plans/{n}") for n in PLANS]
CHAOS = pair("docs/chaos-report.md", "docs/reports/chaos-report.md")
KEEL = pair("FRAMEWORK_REFACTOR_PLAN.md", "docs/plans/FRAMEWORK_REFACTOR_PLAN.md")
AUDIT = pair("CODE_QUALITY_AUDIT.md", "docs/plans/CODE_QUALITY_AUDIT.md")
TRANS = pair("TRANSFORMATION_PLAN.md", "docs/plans/TRANSFORMATION_PLAN.md")

# AGENTS.md 规划类文档一节补登记新计划（插在 CHANGELOG 条目之前）
INSERT_DIFF = pair(
    "- `CHANGELOG.md` — 已发布内容，写 release 前更新",
    "- `docs/plans/DIFFERENTIATION_PLAN.md` — 求职冲刺阶段（A–D）总方案与执行顺序\n"
    "- `CHANGELOG.md` — 已发布内容，写 release 前更新",
)

FIXES = {
    "PROMPTS.md": PLAN_FIX + [CHAOS],
    "AGENTS.md": PLAN_FIX[:3] + [INSERT_DIFF],
    "CHANGELOG.md": [CHAOS],
    "SECURITY.md": [AUDIT],
    "scripts/chaos.py": [CHAOS],
    "docs/architecture/data-flow-and-agent-loop.md": [CHAOS],
    "docs/architecture/overview.md": [KEEL],
    "docs/development/agent-loop-walkthrough.md": [AUDIT],
    "docs/specs/2026-08-29-p0-security-fixes.md": [AUDIT],
    "docs/specs/2026-08-29-keel-r1-r4-detailed-plan.md": [KEEL],
    "docs/specs/2026-08-29-sandbox-p1a-adr.md": [TRANS],
    "docs/specs/2026-08-29-sandbox-p2a-adr.md": [TRANS],
    "docs/plans/TRANSFORMATION_PLAN.md": [CHAOS],
    "docs/plans/DIFFERENTIATION_PLAN.md": [CHAOS],
    "docs/plans/refactor-plan-akashic.md": [
        pair(r"C:\Users\7\Desktop\星环Agent\akashic-agent-main", "akashic-agent（开源参考项目）"),
    ],
}

for rel, fixes in FIXES.items():
    path = ROOT / rel
    data = path.read_bytes()
    report = []
    for old, new in fixes:
        n = data.count(old)
        if n:
            data = data.replace(old, new)
        report.append(f"{old.decode('utf-8', 'replace')[:32]}… → {n} 处")
    path.write_bytes(data)
    print(f"{rel}:\n  " + "\n  ".join(report))
```

要点：

- 精确匹配"文件名.md"全串，不带 .md 的纯名提及（如 AGENTS.md 里"确认 TRANSFORMATION_PLAN
  各阶段"）保持不动——文件名没变，只补路径前缀，语义无损
- `docs/plans/` 内部互引保持裸文件名（同目录），脚本不涉及
- 每个替换输出计数，**任何"→ 0 处"都要人工检查**（说明预期引用点不存在，可能移错了文件）
- AGENTS.md 被 gitignore，改它是本地生效、不进 commit（改它是因为它仍被每个会话读到）

### 步骤 2：contributing.md 重复判定

`CONTRIBUTING.md`（根，23 行）与 `docs/development/contributing.md`（4 行）内容不同。
看 4 行版内容：若是指向根版的指针 → 保留不动；若自成重复内容 → `git rm
docs/development/contributing.md`，并把 AGENTS.md 文档阅读地图"新人上手"行中的
`contributing.md` 一并去掉。

### 步骤 3：核验清单（命令 + 预期）

```bash
# 1) 旧路径零残留（三条都应无输出）
grep -rn "docs/chaos-report.md" --include="*.md" --include="*.py" . | grep -v "\.git/"
grep -rn "C:\\\\Users" docs/ | grep -v "\.git/"
grep -rn --include="*.md" -e "TRANSFORMATION_PLAN.md" -e "FRAMEWORK_REFACTOR_PLAN.md" \
  -e "PROJECT_REVIEW.md" -e "CODE_QUALITY_AUDIT.md" -e "DIFFERENTIATION_PLAN.md" . \
  | grep -v "\.git/" | grep -v "docs/plans/" | grep -v "docs/career/"

# 2) 结构核验：docs 根只剩 7 个子目录（api architecture career development plans reports specs），无散件
ls docs/

# 3) 一次性脚本删除
rm scripts/fix_doc_refs.py
```

### 步骤 4：回归自检

只动了 markdown 与 `scripts/chaos.py` 的注释行，但按仓库提交前自检惯例跑：
`ruff check && ruff format --check && pytest tests/unit -q` 全绿。

### 步骤 5：提交（需用户确认后执行）

`git add` 范围：全部 M / R / D + `docs/plans/DIFFERENTIATION_PLAN.md` +
`docs/career/.gitignore`（career 其余内容被 ignore 不会进来）。

建议单 commit（同一逻辑变更）：

```
docs: 文档体系重组——计划/报告归位，个人材料退出仓库

- 根目录门面化：5 个计划/审查文档迁入 docs/plans/，chaos-report 迁入 docs/reports/
- 简历写法/量化测试/study 迁入 docs/career/ 并解除 git 跟踪（求职材料不入公开仓库）
- 全库约 40 处交叉引用同步（AGENTS.md/PROMPTS.md/架构文档/specs ADR/CHANGELOG/chaos.py）
- 清理 refactor-plan-akashic 中的本机路径泄露；.gitignore 同步 migration-plan 新路径
```

（注：PROMPTS.md 里上一任务的"求职冲刺阶段 A–D 登记"改动会随本 commit 一并进入；
若要拆成两个 commit 需 `git add -p` 手工分离，通常不值得。）

## 三、验收标准

- [ ] 根目录只剩：README / CHANGELOG / CONTRIBUTING / SECURITY / LICENSE / AGENTS.md（本地）/ PROMPTS.md
- [ ] docs/ 根只有 7 个子目录，无散件
- [ ] 步骤 3 三条 grep 全部零输出
- [ ] 修复脚本每处计数 ≥1（无"→ 0 处"存疑项）
- [ ] ruff + pytest tests/unit 全绿
- [ ] git status 与预期清单一致（5 R + 2 D + 若干 M + 2 新增）

## 四、回滚方式

- 提交前：`git reset`（撤销暂存）+ 手动把文件移回原位（career 的 study 是普通 mv，
  git 不管它，需手动移回）
- 提交后：`git revert <commit>` 最干净——所以**建议先 commit 再审视 diff**

## 五、待决问题（默认不动，用户拍板）

1. **AGENTS.md 被 .gitignore 排除**：公开仓库看不到开发规范与文档路由表，等于把最能
   体现工程素养的一份文档藏起来了。若取消 ignore：删 .gitignore 该节两行中 AGENTS.md
   一行 + `git add AGENTS.md`（AGENTS.md 中"求职冲刺阶段"等表述若介意可先润色）。
2. **PROMPTS.md 随仓库公开**：含"求职冲刺阶段 A–E"字样，与 DIFFERENTIATION_PLAN 一样
   带求职语境。若介意：这两处挪到 gitignore 的本地文件，PROMPTS 只留 P/R 阶段。
3. `docs/specs/2026-08-29-baseline.md` 是否挪 `docs/reports/`：默认不挪（ADR 目录混入
   一篇基线报告属轻微不纯，改动收益低）。
