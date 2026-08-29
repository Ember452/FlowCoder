"""sandbox_mode 配置校验、加载与用户配置持久化的测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowcoder.config import (
    AppConfig,
    ConfigError,
    load_config,
    update_user_config_value,
    validate_sandbox_mode,
)


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


class TestValidateSandboxMode:
    def test_accepts_known_modes(self) -> None:
        assert validate_sandbox_mode("off") == "off"
        assert validate_sandbox_mode("docker") == "docker"

    def test_rejects_unknown(self) -> None:
        with pytest.raises(ConfigError, match="sandbox_mode"):
            validate_sandbox_mode("k8s")


class TestLoadConfig:
    def test_default_is_off(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_config(path)
        cfg = load_config(path)
        assert cfg.sandbox_mode == "off"
        assert not cfg.sandbox_mode_declared

    def test_declared_docker(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_config(path, "sandbox_mode: docker\n")
        cfg = load_config(path)
        assert cfg.sandbox_mode == "docker"
        assert cfg.sandbox_mode_declared

    def test_invalid_value_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_config(path, "sandbox_mode: k8s\n")
        with pytest.raises(ConfigError, match="sandbox_mode"):
            load_config(path)

    def test_merge_layer_overrides(self) -> None:
        from flowcoder.config.core import _merge_config

        base = AppConfig(providers=[])
        base.sandbox_mode = "off"
        override = AppConfig(providers=[])
        override.sandbox_mode = "docker"
        override.sandbox_mode_declared = True
        merged = _merge_config(base, override)
        assert merged.sandbox_mode == "docker"

        undeclared = AppConfig(providers=[])
        merged2 = _merge_config(base, undeclared)
        assert merged2.sandbox_mode == "docker"  # 未声明不覆盖


class TestUpdateUserConfigValue:
    def test_appends_to_missing_file(self, tmp_path: Path) -> None:
        target = tmp_path / ".flowcoder" / "config.yaml"
        returned = update_user_config_value("sandbox_mode", "docker", path=target)
        assert returned == target
        assert target.read_text(encoding="utf-8") == "sandbox_mode: docker\n"

    def test_replaces_existing_line(self, tmp_path: Path) -> None:
        target = tmp_path / "config.yaml"
        target.write_text("# 我的配置\nsandbox_mode: off\nproviders: []\n", encoding="utf-8")
        update_user_config_value("sandbox_mode", "docker", path=target)
        content = target.read_text(encoding="utf-8")
        assert "sandbox_mode: docker" in content
        assert "sandbox_mode: off" not in content
        assert "# 我的配置" in content  # 注释保留

    def test_appends_when_key_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "config.yaml"
        target.write_text("providers: []\n", encoding="utf-8")
        update_user_config_value("sandbox_mode", "docker", path=target)
        content = target.read_text(encoding="utf-8")
        assert content.startswith("sandbox_mode: docker\n")

    def test_does_not_touch_indented_keys(self, tmp_path: Path) -> None:
        target = tmp_path / "config.yaml"
        target.write_text("worktree:\n  sandbox_mode: docker\n", encoding="utf-8")
        update_user_config_value("sandbox_mode", "off", path=target)
        content = target.read_text(encoding="utf-8")
        # 顶层键追加，嵌套的同名键不受影响
        assert content.startswith("sandbox_mode: off\n")
        assert "  sandbox_mode: docker" in content

    def test_writes_roundtrip_via_load(self, tmp_path: Path) -> None:
        target = tmp_path / "config.yaml"
        _write_config(target)
        update_user_config_value("sandbox_mode", "docker", path=target)
        cfg = load_config(target)
        assert cfg.sandbox_mode == "docker"
