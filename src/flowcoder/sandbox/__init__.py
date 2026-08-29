"""Docker 沙箱包：单容器执行、资源限额、默认断网、文件传输、容器池化与泄漏回收。"""

from flowcoder.sandbox.container import (
    EXEC_MARGIN_S,
    NON_ROOT_USER,
    ExecutionResult,
    SandboxConfig,
    SandboxContainer,
)
from flowcoder.sandbox.limits import ResourceLimits
from flowcoder.sandbox.metrics import SandboxMetrics
from flowcoder.sandbox.pool import (
    SANDBOX_LABEL,
    Lease,
    LeaseRecord,
    PoolExhaustedError,
    SandboxPool,
    TraceSink,
)
from flowcoder.sandbox.reaper import LeaseReaper
from flowcoder.sandbox.runtime import (
    ContainerRuntime,
    DockerRuntime,
    ExecOutcome,
    SandboxError,
)
from flowcoder.sandbox.transport import build_tar_bytes, copy_files

__all__ = [
    "EXEC_MARGIN_S",
    "SANDBOX_LABEL",
    "ContainerRuntime",
    "DockerRuntime",
    "ExecOutcome",
    "ExecutionResult",
    "Lease",
    "LeaseRecord",
    "LeaseReaper",
    "NON_ROOT_USER",
    "PoolExhaustedError",
    "ResourceLimits",
    "SandboxConfig",
    "SandboxContainer",
    "SandboxError",
    "SandboxMetrics",
    "SandboxPool",
    "TraceSink",
    "build_tar_bytes",
    "copy_files",
]
