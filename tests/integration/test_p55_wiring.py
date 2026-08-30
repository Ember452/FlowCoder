"""P5.5 接线测试：scheduler/watchdog/budget 三段的配置、装配与零配置零变化。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from flowcoder.config import AppConfig, ConfigError, ProviderConfig
from flowcoder.config.core import BudgetConfig, SchedulerConfig
from flowcoder.config.validator import validate_budget, validate_watchdog
from flowcoder.daemon.background import build_budget_for_agent
from flowcoder.daemon.server import create_app
from flowcoder.daemon.session.store import SessionStore
from flowcoder.permissions import PermissionMode


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="fake",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/v1",
        model="fake-model",
    )


class TestConfigParsing:
    def test_scheduler_config_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "providers": [_provider().__dict__],
                    "scheduler": {
                        "enabled": True,
                        "jobs": [{"name": "nightly", "cron": "0 2 * * *", "prompt": "夜间巡检"}],
                    },
                }
            ),
            encoding="utf-8",
        )
        from flowcoder.config import load_config

        cfg = load_config(path)
        assert cfg.scheduler.enabled
        assert cfg.scheduler.jobs[0].name == "nightly"
        assert cfg.watchdog.enabled is False
        assert cfg.budget is None

    def test_scheduler_invalid_cron_rejected_at_load(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "providers": [_provider().__dict__],
                    "scheduler": {
                        "enabled": True,
                        "jobs": [{"name": "x", "cron": "99 * * * *", "prompt": "p"}],
                    },
                }
            ),
            encoding="utf-8",
        )
        from flowcoder.config import load_config

        with pytest.raises(ConfigError, match="cron"):
            load_config(path)

    def test_watchdog_defaults(self) -> None:
        validated = validate_watchdog(None)
        assert validated["enabled"] is False
        assert validated["poll_interval_s"] == 300
        assert validated["cooldown_s"] == 1800.0

    def test_budget_validation(self) -> None:
        with pytest.raises(ConfigError, match="至少"):
            validate_budget({})
        with pytest.raises(ConfigError, match="单价"):
            validate_budget({"max_cost_usd": 5.0})
        result = validate_budget({"max_total_tokens": 100_000, "input_price_per_1m": 3.0})
        assert result["max_total_tokens"] == 100_000.0

    def test_default_appconfig_has_no_background_services(self) -> None:
        cfg = AppConfig(providers=[_provider()])
        assert cfg.scheduler.enabled is False
        assert cfg.watchdog.enabled is False
        assert cfg.budget is None


class TestBudgetWiring:
    async def test_build_budget_from_config(self) -> None:
        cfg = AppConfig(providers=[_provider()])
        assert await build_budget_for_agent(cfg) is None  # 未配置
        from flowcoder.config.core import BudgetConfig

        cfg.budget = BudgetConfig(max_total_tokens=50_000)
        budget = await build_budget_for_agent(cfg)
        assert budget is not None
        assert budget.max_total_tokens == 50_000

    async def test_agent_factory_injects_budget(self, tmp_path: Path) -> None:
        from flowcoder.agent.factory import create_agent_from_config

        cfg = AppConfig(providers=[_provider()], budget=BudgetConfig(max_turns=3))
        agent, _deps = await create_agent_from_config(
            cfg, work_dir=str(tmp_path), permission_mode=PermissionMode.BYPASS
        )
        assert agent._budget_state is not None
        # 轮次预算生效：第 4 轮开头应判定超限
        assert (
            agent._budget_state.breach_reason(
                total_input_tokens=0, total_output_tokens=0, iteration=4
            )
            is not None
        )

    async def test_agent_factory_without_budget_unchanged(self, tmp_path: Path) -> None:
        from flowcoder.agent.factory import create_agent_from_config

        cfg = AppConfig(providers=[_provider()])
        agent, _deps = await create_agent_from_config(
            cfg, work_dir=str(tmp_path), permission_mode=PermissionMode.BYPASS
        )
        assert agent._budget_state is None  # 零配置零变化


class TestDaemonWiring:
    def test_zero_config_starts_no_background_services(self, tmp_path: Path) -> None:
        """零配置零变化：不创建调度器状态文件（装配未发生）。"""
        app = create_app(
            AppConfig(providers=[_provider()]),
            str(tmp_path),
            session_store=SessionStore(tmp_path / "sessions"),
        )
        with TestClient(app):
            pass
        assert not (tmp_path / ".flowcoder" / "scheduler.json").exists()
        assert not (tmp_path / ".flowcoder" / "watchdog.json").exists()

    def test_scheduler_jobs_registered_on_startup(self, tmp_path: Path) -> None:
        from flowcoder.config.core import ScheduledJobConfig

        cfg = AppConfig(
            providers=[_provider()],
            scheduler=SchedulerConfig(
                enabled=True,
                jobs=[ScheduledJobConfig(name="daily", cron="0 3 * * *", prompt="夜间任务")],
            ),
            scheduler_declared=True,
        )
        app = create_app(
            cfg,
            str(tmp_path),
            session_store=SessionStore(tmp_path / "sessions"),
        )
        with TestClient(app):
            # lifespan 装配了调度器：任务注册进状态文件，且不会立即触发
            state_file = tmp_path / ".flowcoder" / "scheduler.json"
            assert state_file.exists()
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            assert "daily" in raw["jobs"]
            assert raw["jobs"]["daily"]["cron"] == "0 3 * * *"
            assert raw["states"]["daily"]["next_run"] is not None

    def test_watchdog_state_file_created_on_startup(self, tmp_path: Path) -> None:
        from flowcoder.config.core import WatchdogConfig

        cfg = AppConfig(
            providers=[_provider()],
            watchdog=WatchdogConfig(enabled=True, watch_git=False, paths=[]),
            watchdog_declared=True,
        )
        app = create_app(
            cfg,
            str(tmp_path),
            session_store=SessionStore(tmp_path / "sessions"),
        )
        with TestClient(app):
            # 看门狗装配（watch_git=False、无 paths 时 _build_watchdog 返回 None →
            # 状态文件不应出现）
            pass
        assert not (tmp_path / ".flowcoder" / "watchdog.json").exists()

    def test_watchdog_with_file_source(self, tmp_path: Path) -> None:
        from flowcoder.config.core import WatchdogConfig

        watched = tmp_path / "watch.txt"
        watched.write_text("v1", encoding="utf-8")
        cfg = AppConfig(
            providers=[_provider()],
            watchdog=WatchdogConfig(enabled=True, watch_git=False, paths=[str(watched)]),
            watchdog_declared=True,
        )
        app = create_app(
            cfg,
            str(tmp_path),
            session_store=SessionStore(tmp_path / "sessions"),
        )
        with TestClient(app):
            # 装配成功即可：首轮 poll 无变更信号；状态文件在首次送达时才写
            pass
