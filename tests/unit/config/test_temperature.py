"""temperature 配置（P2b 可复现性）测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowcoder.config import ConfigError, load_config
from flowcoder.config.validator import validate_providers


def _write_config(path: Path, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "providers:\n"
        "  - name: test\n"
        "    protocol: openai\n"
        "    base_url: http://test.local/v1\n"
        "    model: test-model\n"
        f"{extra}",
        encoding="utf-8",
    )


class TestTemperatureConfig:
    def test_default_none(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_config(path)
        cfg = load_config(path)
        assert cfg.providers[0].temperature is None

    def test_declared_temperature(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_config(path, "    temperature: 0.0\n")
        cfg = load_config(path)
        assert cfg.providers[0].temperature == 0.0

    @pytest.mark.parametrize("bad", ["3.5", '"hot"', "true"])
    def test_invalid_temperature_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ConfigError, match="temperature"):
            validate_providers(
                [
                    {
                        "name": "p",
                        "protocol": "openai",
                        "base_url": "http://x/v1",
                        "model": "m",
                        "api_key": "",
                        "thinking": False,
                        "temperature": bad,
                        "context_window": 0,
                        "max_output_tokens": 0,
                    }
                ]
            )

    def test_request_builder_includes_temperature(self) -> None:
        from flowcoder.providers.anthropic_request import build_anthropic_request_kwargs
        from flowcoder.providers.openai_compat_request import (
            build_chat_completion_request_kwargs,
        )

        kwargs = build_anthropic_request_kwargs(
            model="m", max_output_tokens=10, messages=[], temperature=0.2
        )
        assert kwargs["temperature"] == 0.2

        # thinking 模式与 temperature 互斥（Anthropic API 约束）
        kwargs_thinking = build_anthropic_request_kwargs(
            model="m", max_output_tokens=10, messages=[], thinking=True, temperature=0.2
        )
        assert "temperature" not in kwargs_thinking

        chat_kwargs = build_chat_completion_request_kwargs(
            model="m", max_output_tokens=10, messages=[], temperature=0.0
        )
        assert chat_kwargs["temperature"] == 0.0

    def test_temperature_omitted_when_none(self) -> None:
        from flowcoder.providers.anthropic_request import build_anthropic_request_kwargs

        kwargs = build_anthropic_request_kwargs(model="m", max_output_tokens=10, messages=[])
        assert "temperature" not in kwargs
