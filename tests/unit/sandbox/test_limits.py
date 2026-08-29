"""ResourceLimits 与容器级限额参数翻译测试。"""

from __future__ import annotations

import pytest

from flowcoder.sandbox.limits import ResourceLimits, container_kwargs


class TestResourceLimits:
    def test_defaults(self) -> None:
        limits = ResourceLimits()
        assert limits.memory_mb == 256
        assert limits.cpus == 1.0
        assert limits.pids_limit == 128

    @pytest.mark.parametrize(
        ("field", "value"), [("memory_mb", 0), ("cpus", -1.0), ("pids_limit", 0)]
    )
    def test_rejects_non_positive(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            ResourceLimits(**{field: value})


class TestContainerKwargs:
    def test_translation(self) -> None:
        kwargs = container_kwargs(ResourceLimits(memory_mb=512, cpus=2.5, pids_limit=64))
        assert kwargs == {
            "mem_limit": "512m",
            "nano_cpus": 2_500_000_000,
            "pids_limit": 64,
        }

    def test_fractional_cpu(self) -> None:
        kwargs = container_kwargs(ResourceLimits(cpus=0.5))
        assert kwargs["nano_cpus"] == 500_000_000
