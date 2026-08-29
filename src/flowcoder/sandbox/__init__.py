"""Docker 沙箱包：单容器执行、资源限额、默认断网、文件传输（P1a；池化与回收见 P1b）。"""

from flowcoder.sandbox.container import (
    EXEC_MARGIN_S,
    NON_ROOT_USER,
    ExecutionResult,
    SandboxConfig,
    SandboxContainer,
)
from flowcoder.sandbox.limits import ResourceLimits
from flowcoder.sandbox.runtime import (
    ContainerRuntime,
    DockerRuntime,
    ExecOutcome,
    SandboxError,
)
from flowcoder.sandbox.transport import build_tar_bytes, copy_files

__all__ = [
    "EXEC_MARGIN_S",
    "ContainerRuntime",
    "DockerRuntime",
    "ExecOutcome",
    "ExecutionResult",
    "NON_ROOT_USER",
    "ResourceLimits",
    "SandboxConfig",
    "SandboxContainer",
    "SandboxError",
    "build_tar_bytes",
    "copy_files",
]
