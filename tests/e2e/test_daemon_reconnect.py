"""e2e：daemon 断线重连——客户端断开期间会话状态不丢，重连后继续工作。"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from flowcoder.config import AppConfig, ProviderConfig
from flowcoder.daemon.server import create_app
from flowcoder.daemon.session.store import SessionStore

SID = "e2e-reconnect-session"


def _app(tmp_path: Path):
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
    )


def _emit(server, sid: str, event: dict) -> None:
    """模拟任务在客户端离线期间持续产出事件（真实路径经 records.emit 落账）。"""
    server._records.emit(sid, event)
    server._records.persist_events(sid) if hasattr(
        server._records, "persist_events"
    ) else server._records.persist_meta(sid)


def test_reconnect_replays_offline_events_in_order(tmp_path: Path) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        # ① 创建会话并建立连接
        server = app.state.server
        server._records.create(SID, str(tmp_path), provider_name="fake")

        with client.websocket_connect(f"/api/stream/{SID}") as ws:
            assert ws.receive_json() == {"type": "ReplayDone", "data": {}}
        # ② 客户端断开（with 退出即断）

        # ③ 离线期间任务持续产出事件
        offline_events = [
            {"type": "UserMessage", "data": {"content": "hello"}},
            {"type": "StreamText", "data": {"text": "部分回复"}},
            {"type": "LoopComplete", "data": {"total_turns": 1}},
        ]
        for event in offline_events:
            _emit(server, SID, event)

        # ④ 重连：离线事件按序补投，不丢
        with client.websocket_connect(f"/api/stream/{SID}") as ws:
            replayed = [ws.receive_json() for _ in range(len(offline_events))]
            assert replayed == offline_events
            assert ws.receive_json() == {"type": "ReplayDone", "data": {}}
            # ⑤ 重连后会话仍可交互（发送客户端动作不报错、连接不被拒）
            ws.send_json({"action": "cancel"})

        # 会话状态完整：事件台账与元数据都在
        assert server._records.has(SID)
        assert len(server._records.event_logs[SID]) == len(offline_events)


def test_session_survives_server_restart(tmp_path: Path) -> None:
    # 会话元数据/事件持久化：服务重启（新 app 实例，同一 session_store）后可恢复
    app1 = _app(tmp_path)
    with TestClient(app1):
        server = app1.state.server
        server._records.create(SID, str(tmp_path), provider_name="fake")
        server._records.emit(SID, {"type": "UserMessage", "data": {"content": "persist me"}})
        server._records.persist_meta(SID)
        server._records.persist_events(SID)  # 事件台账落盘

    app2 = _app(tmp_path)
    with TestClient(app2):
        server2 = app2.state.server
        server2._records.load_persisted()
        assert server2._records.has(SID), "重启后会话应从磁盘恢复"
        log = server2._records.event_logs[SID]
        assert any(
            isinstance(e, dict) and e.get("data", {}).get("content") == "persist me" for e in log
        )
