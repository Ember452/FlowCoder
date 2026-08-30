"""FlowCoder 包入口。

本地优先的 AI 编程助手运行时根包。
子包/模块覆盖 Agent 循环、工具、权限、上下文、记忆、MCP、TUI 与 Daemon 等。
"""

from importlib import metadata

try:
    __version__: str = metadata.version("flowcoder")
except metadata.PackageNotFoundError:  # 未安装（源码直跑）时回退
    __version__ = "0.6.0"
