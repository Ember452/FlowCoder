"""Outbox 流路由集成测试：since 断点补投、ack 记账、结果未知不重放（P5c）。"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from flowcoder.config import AppConfig, ProviderConfig
from flowcoder.daemon.server import create_app
from flowcoder.daemon.session.store import SessionStore

SID = "outbox-session"


def _app(tmp_path: Path, *, retention_s: float | None = 72 * 3600.0):
    provider = ProviderConfig(
        name="fake",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/v1",
        model="fake-model",
    )
    return create_app(
        AppConfig(providers=[provider]),
        str(tmp_path),
        session_store=SessionStore(tmp_path / "sessions"),
        outbox_retention_s=retention_s,
    )


def _seed(app, events: list[dict]) -> None:
    server = app.state.server
    if not server._records.has(SID):
        server._records.create(SID, str(app.state.server.work_dir))
    for event in events:
        server._records.emit(SID, dict(event))
    server._records.persist_events(SID)


class TestSinceReplay:
    def test_since_skips_older_events(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        _seed(app, [{"type": "UserMessage", "data": {}}, {"type": "StreamText", "data": {}}])

        with TestClient(app) as client:
            with client.websocket_connect(f"/api/stream/{SID}?since=1") as ws:
                first = ws.receive_json()
                assert first["seq"] == 2  # seq=1 的事件不重放
                assert ws.receive_json() == {"type": "ReplayDone", "data": {}}

    def test_replay_envelope_carries_seq(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        _seed(app, [{"type": "UserMessage", "data": {}}, {"type": "TurnComplete", "data": {}}])
        with TestClient(app) as client:
            with client.websocket_connect(f"/api/stream/{SID}") as ws:
                first = ws.receive_json()
                assert first["seq"] == 1
                second = ws.receive_json()
                assert second["seq"] == 2

    def test_ack_persists_and_resume_without_since(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        _seed(
            app, [{"type": "A", "data": {}}, {"type": "B", "data": {}}, {"type": "C", "data": {}}]
        )

        with TestClient(app) as client:
            with client.websocket_connect(f"/api/stream/{SID}") as ws:
                assert ws.receive_json()["seq"] == 1
                assert ws.receive_json()["seq"] == 2
                ws.send_json({"action": "ack", "seq": 2})
            # "断线"后重连（不带 since）：从持久化 ack 之后补投
            with client.websocket_connect(f"/api/stream/{SID}") as ws:
                first = ws.receive_json()
                assert first["seq"] == 3  # 1、2 已确认不重投
                assert ws.receive_json() == {"type": "ReplayDone", "data": {}}

        # ack 持久化：新 app 实例（重启）仍记得
        app2 = _app(tmp_path)
        assert app2.state.server.outbox_ack(SID) == 2

    def test_ack_survives_restart(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        _seed(app, [{"type": "A", "data": {}}])
        with TestClient(app) as client:
            with client.websocket_connect(f"/api/stream/{SID}") as ws:
                ws.receive_json()
                ws.send_json({"action": "ack", "seq": 1})
        app2 = _app(tmp_path)
        assert app2.state.server.outbox_ack(SID) == 1


class TestNoReplayOnUnknown:
    def test_pushed_interactive_event_not_replayed(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        _seed(
            app,
            [
                {"type": "StreamText", "data": {}},
                {"type": "PermissionRequest", "data": {"request_id": "r1"}},
            ],
        )
        with TestClient(app) as client:
            # 首次连接：两个事件都推送（PermissionRequest 进入"结果未知"账本）
            with client.websocket_connect(f"/api/stream/{SID}") as ws:
                assert ws.receive_json()["seq"] == 1
                assert ws.receive_json()["seq"] == 2
            # 断开（未 ack）

            # 重连带 since=0：StreamText 重放（幂等），
            # PermissionRequest 结果未知 → 跳过（不重放）
            with client.websocket_connect(f"/api/stream/{SID}?since=0") as ws:
                seen = []
                while True:
                    event = ws.receive_json()
                    if event["type"] == "ReplayDone":
                        break
                    seen.append(event)
            assert [e["seq"] for e in seen] == [1]
            assert all(e["type"] != "PermissionRequest" for e in seen)


class TestRetentionCleanup:
    def test_cleanup_drops_only_acked_expired(self, tmp_path: Path) -> None:
        import time

        app = _app(tmp_path, retention_s=100.0)
        _seed(app, [{"type": "Old", "data": {}}, {"type": "New", "data": {}}])
        server = app.state.server
        server.ack_outbox(SID, 1)  # 只有 seq=1 被确认
        # 把 seq=1 的时间戳改老
        import json as json_mod

        path = server.outbox_events_path(SID)
        lines = path.read_text(encoding="utf-8").splitlines()
        rewritten = []
        for line in lines:
            event = json_mod.loads(line)
            if event.get("seq") == 1:
                event["ts"] = time.time() - 10_000
            rewritten.append(json_mod.dumps(event))
        path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

        dropped = server.cleanup_outbox()
        assert dropped.get(SID) == 1  # 只删过期的已投递事件

        with TestClient(app) as client:
            # 未 ack 的事件（seq=2，虽然也是刚写入的）不能被误删
            with client.websocket_connect(f"/api/stream/{SID}") as ws:
                first = ws.receive_json()
                assert first["seq"] == 2
