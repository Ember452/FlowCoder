"""池化指标：租借等待、容器复用、执行耗时、资源峰值（P1b）。

只保留聚合值（总量/峰值），不保留逐次明细，避免长期运行内存无界增长。
接入 TraceManager 的方式见 pool.py 的 TraceSink Protocol——sandbox 不反向依赖 agents。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SandboxMetrics:
    """池生命周期内的聚合指标。"""

    #: 完成的租借次数（不含仍在租借中的）
    leases: int = 0
    #: 执行总次数
    executions: int = 0
    #: 复用次数：同一容器第二次及以后的执行（一次租借内多次执行即复用）
    reuse_count: int = 0
    lease_wait_total_ms: int = 0
    lease_wait_max_ms: int = 0
    exec_duration_total_ms: int = 0
    exec_duration_max_ms: int = 0
    #: 采样到的资源峰值（best-effort，容器刚死时 stats 可能取不到）
    peak_memory_mb: float = 0.0
    peak_cpu_percent: float = 0.0

    def record_lease_wait(self, wait_ms: int) -> None:
        self.lease_wait_total_ms += wait_ms
        self.lease_wait_max_ms = max(self.lease_wait_max_ms, wait_ms)

    def record_execution(self, duration_ms: int, *, reused: bool) -> None:
        self.executions += 1
        if reused:
            self.reuse_count += 1
        self.exec_duration_total_ms += duration_ms
        self.exec_duration_max_ms = max(self.exec_duration_max_ms, duration_ms)

    def record_resource(self, memory_mb: float, cpu_percent: float) -> None:
        self.peak_memory_mb = max(self.peak_memory_mb, memory_mb)
        self.peak_cpu_percent = max(self.peak_cpu_percent, cpu_percent)

    def record_lease_released(self) -> None:
        self.leases += 1

    def snapshot(self, *, pool_size: int, idle: int, active: int) -> dict[str, int | float]:
        """当前指标快照，含池水位 gauge。"""
        return {
            "pool_size": pool_size,
            "idle": idle,
            "active": active,
            "leases": self.leases,
            "executions": self.executions,
            "reuse_count": self.reuse_count,
            "lease_wait_total_ms": self.lease_wait_total_ms,
            "lease_wait_max_ms": self.lease_wait_max_ms,
            "exec_duration_total_ms": self.exec_duration_total_ms,
            "exec_duration_max_ms": self.exec_duration_max_ms,
            "peak_memory_mb": self.peak_memory_mb,
            "peak_cpu_percent": self.peak_cpu_percent,
        }
