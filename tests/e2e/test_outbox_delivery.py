"""e2e：Outbox 压测验收（P5c）——推送中途 kill 客户端，重连后事件序列完整。

场景：客户端消费到一半被"kill"（直接断开、未 ack），daemon 继续产出事件
（全部落 outbox 持久化）；重连带 since 游标断点补投。校验：
- 不丢：重连收到的 seq 与断开前收到的 seq 合并后连续覆盖全部已产生事件；
- 不重：补投起点严格大于客户端已收到的最大 seq；
- 交互事件"结果未知"不重放。
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from flowcoder.config import AppConfig, ProviderConfig
from flowcoder.daemon.server import create_app
from flowcoder.daemon.session.store import SessionStore

SID = "chaos-outbox-session"


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
        outbox_retention_s=72 * 3600.0,
    )


def _emit_batch(app, n: int, tag: str) -> None:
    server = app.state.server
    for i in range(n):
        server.emit_event(SID, {"type": "StreamText", "data": {"text": f"{tag}-{i}"}})
    server._records.persist_events(SID)


def test_kill_midstream_reconnect_complete_sequence(tmp_path: Path) -> None:
    app = _app(tmp_path)
    server = app.state.server
    server._records.create(SID, str(tmp_path))

    with TestClient(app) as client:
        # ① 第一段：客户端正常消费 5 个事件（seq 1..5）
        received: list[int] = []
        with client.websocket_connect(f"/api/stream/{SID}") as ws:
            assert ws.receive_json()["type"] == "ReplayDone"
            _emit_batch(app, 5, "first")
            for _ in range(5):
                received.append(ws.receive_json()["seq"])

        # ② kill：未 ack 直接断开（客户端已收到 seq 1..5，但服务器不知道）
        assert received == [1, 2, 3, 4, 5]

        # ③ daemon 继续产出（推给空气，全部落 outbox）
        _emit_batch(app, 5, "second")

        # ④ 重连带 since=5（客户端自己记录的游标）：断点补投 6..10
        with client.websocket_connect(f"/api/stream/{SID}?since=5") as ws:
            replayed = []
            while True:
                event = ws.receive_json()
                if event["type"] == "ReplayDone":
                    break
                replayed.append(event["seq"])
        assert replayed == [6, 7, 8, 9, 10]  # 不丢不重

        # ⑤ 客户端 ack 到 10，再重连（无 since）：从持久化 ack 之后补投，空
        with client.websocket_connect(f"/api/stream/{SID}") as ws:
            ws.send_json({"action": "ack", "seq": 10})
        with client.websocket_connect(f"/api/stream/{SID}") as ws:
            assert ws.receive_json()["type"] == "ReplayDone"

        # ⑥ 完整性：客户端视角的 seq 并集 = 1..10 连续，无缺口无重复
        assert sorted(received + replayed) == list(range(1, 11))


def test_interactive_event_unknown_delivery_skipped_on_reconnect(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    server = app.state.server
    server._records.create(SID, str(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect(f"/api/stream/{SID}") as ws:
            assert ws.receive_json()["type"] == "ReplayDone"
            # 推送一个交互事件（结果未知）+ 一个普通事件
            server.emit_event(SID, {"type": "PermissionRequest", "data": {"request_id": "r1"}})
            server.emit_event(SID, {"type": "StreamText", "data": {"text": "after"}})
            server._records.persist_events(SID)
            e1 = ws.receive_json()
            e2 = ws.receive_json()
            assert e1["type"] == "PermissionRequest"
            assert e2["type"] == "StreamText"

        # kill 后重连（since=0 全量补投视角）：交互事件结果未知 → 跳过，
        # 普通事件重放
        with client.websocket_connect(f"/api/stream/{SID}?since=0") as ws:
            replayed = []
            while True:
                event = ws.receive_json()
                if event["type"] == "ReplayDone":
                    break
                replayed.append(event)
        assert [e["type"] for e in replayed] == ["StreamText"]  # 交互事件被跳过
