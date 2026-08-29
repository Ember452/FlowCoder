"""失败四分类：编译错 / 逻辑错 / 测试理解错 / 超预算。

分类是启发式，仅基于已有输出特征（stderr、可编译性、超时/轮次标志），
附每条规则的依据与局限：

- 编译错：生成的代码过不了 compile()，或 stderr 出现语法类异常。
  局限：exec 动态生成的语法错误也计入（少见）。
- 超预算：执行超时，或自愈轮次用尽仍未通过（重试预算耗尽）。
  局限：轮次耗尽可能混有"模型反复给出等价错误解"的情形。
- 逻辑错：断言失败（AssertionError）——代码跑得通但算出了错误值。
  这是"程序逻辑错误"的最直接信号。
- 测试理解错：非断言的运行时异常（TypeError / KeyError / AttributeError /
  IndexError / NameError / ValueError / RecursionError 等）。这类异常通常
  源于误解函数签名、参数形态或返回类型约定——即对题意/测试的理解偏差。
  局限：纯启发式代理；真实"理解错"与"逻辑错"的边界无法仅凭输出特征完全
  区分（例如 off-by-one 也可能以 TypeError 形式暴露）。

分类入口 classify_failure 只对"已执行且未通过"的题返回类别；
skipped / 生成阶段错误（gen_error）返回 None，由 metrics 单列。
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flowcoder.eval.runner import ProblemResult

FAIL_COMPILE = "编译错"
FAIL_LOGIC = "逻辑错"
FAIL_SPEC = "测试理解错"
FAIL_BUDGET = "超预算"

FAIL_CATEGORIES = (FAIL_COMPILE, FAIL_LOGIC, FAIL_SPEC, FAIL_BUDGET)

#: 语法类异常关键字（stderr 特征）
_SYNTAX_MARKERS = ("SyntaxError", "IndentationError", "TabError")

#: 非断言的运行时异常类型名（stderr 特征）——测试理解错的代理信号
_MISUSE_EXCEPTIONS = frozenset(
    {
        "TypeError",
        "KeyError",
        "AttributeError",
        "IndexError",
        "NameError",
        "ValueError",
        "RecursionError",
        "StopIteration",
        "UnboundLocalError",
    }
)


def _compiles(code: str) -> bool:
    try:
        compile(code, "<solution>", "exec")
    except SyntaxError:
        return False
    except (ValueError, OverflowError):  # compile 的罕见非语法失败
        return False
    return True


def _stderr_exception_type(stderr: str) -> str | None:
    """从 traceback 最后一行提取异常类名。"""
    last_line = stderr.strip().rsplit("\n", maxsplit=1)[-1].strip()
    if ":" not in last_line:
        return None
    name = last_line.split(":", maxsplit=1)[0].strip()
    # 形如 "module.SubError" 的取末段
    return name.rsplit(".", maxsplit=1)[-1] or None


def classify_failure(result: ProblemResult, *, rounds_exhausted: bool) -> str | None:
    """对已执行且未通过的结果做四分类；无法分类时返回 None。"""
    if result.skipped or result.passed or result.gen_error is not None:
        return None

    # ① 超预算：执行超时（无有效失败信息可分类）
    if result.timed_out:
        return FAIL_BUDGET

    # ② 编译错：代码过不了 compile，或运行期语法类异常
    if result.generated_code and not _compiles(result.generated_code):
        return FAIL_COMPILE
    if any(marker in result.stderr for marker in _SYNTAX_MARKERS):
        return FAIL_COMPILE

    # ③ 超预算：修复轮次用尽仍未通过（重试预算耗尽）
    if rounds_exhausted:
        return FAIL_BUDGET

    # ④/⑤ 按运行时异常类型区分逻辑错与测试理解错
    exc = _stderr_exception_type(result.stderr)
    if exc == "AssertionError":
        return FAIL_LOGIC
    if exc is not None and exc in _MISUSE_EXCEPTIONS:
        return FAIL_SPEC
    # 内置异常但不在代理清单里（如 ZeroDivisionError）→ 归逻辑错
    if exc is not None and hasattr(builtins, exc):
        return FAIL_LOGIC
    # stderr 无特征（如静默 exit 1 / os._exit）→ 兜底逻辑错
    return FAIL_LOGIC
