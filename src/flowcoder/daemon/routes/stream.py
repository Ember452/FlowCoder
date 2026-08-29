"""事件流路由。

回放会话历史事件并实时 tail；解析客户端动作。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from flowcoder.daemon.outbox import NO_REPLAY_TYPES, event_seq
from flowcoder.daemon.request_context import daemon_server, path_param
from flowcoder.daemon.tasks.events import pending_prompt_request_id

log = logging.getLogger(__name__)

CLIENT_ACTIONS = {"cancel", "ack"}


def event_with_id(event: dict, index: int) -> dict:
    """Attach a stable, session-local event id without changing stored events."""
    if "event_id" in event:
        return event
    return {**event, "event_id": str(index + 1)}


def history_page(events: list[dict], before: int, limit: int) -> tuple[list[dict], int]:
    """Return one history page, aligned to a user-message boundary."""
    end = max(0, min(before, len(events)))
    start = max(0, end - limit)
    fallback_start = start
    while start < end and events[start].get("type") != "UserMessage":
        start += 1
    if start == end:
        start = fallback_start
    return events[start:end], start


def parse_client_action(raw: str) -> str:
    return parse_client_message(raw)[0]


def parse_client_message(raw: str) -> tuple[str, int | None]:
    """解析客户端消息：返回 (action, seq)。ack 动作携带 seq（Outbox P5c）。"""
    if not raw:
        return ("", None)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ("", None)
    if not isinstance(payload, dict):
        return ("", None)
    action = payload.get("action")
    if not isinstance(action, str) or action not in CLIENT_ACTIONS:
        return ("", None)
    seq = payload.get("seq")
    seq_val = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None
    return (action, seq_val)


async def listen_client_actions(
    websocket: WebSocket,
    *,
    server: Any,
    sid: str,
    disconnected: asyncio.Event,
) -> None:
    try:
        while True:
            action, seq = parse_client_message(await websocket.receive_text())
            if action == "cancel":
                server.cancel_active_task(sid)
            elif action == "ack" and seq is not None:
                # Outbox（P5c）：客户端确认已收到 seq，记账并持久化
                server.ack_outbox(sid, seq)
    except WebSocketDisconnect:
        disconnected.set()
    except Exception:
        disconnected.set()


def pending_prompt_events_after_replay(
    pending_events: list[dict],
    replayed_request_ids: set[str],
) -> list[dict]:
    return [
        event
        for event in pending_events
        if pending_prompt_request_id(event) not in replayed_request_ids
    ]


async def stream_events(websocket: WebSocket) -> None:
    """Replay a session's full event history, then tail it live."""
    await websocket.accept()
    sid = path_param(websocket, "sid")
    server = daemon_server(websocket)

    log_list = server.get_event_log(sid)
    if log_list is None:
        await websocket.send_json({"type": "SessionNotFound", "data": {"session_id": sid}})
        await websocket.close(code=4404)
        return

    # Outbox（P5c）：断点重连。since 显式指定游标；缺省用持久化的 ack。
    try:
        since = int(websocket.query_params.get("since", "-1"))
    except ValueError:
        since = -1
    if since < 0:
        since = server.outbox_ack(sid)
    unknown_seqs = server.take_outbox_unknown(sid)

    log.info("WS client connected to session %s (since=%d)", sid, since)
    disconnected = asyncio.Event()
    listener = asyncio.create_task(
        listen_client_actions(
            websocket,
            server=server,
            sid=sid,
            disconnected=disconnected,
        )
    )
    try:
        requested_limit = int(websocket.query_params.get("limit", "0"))
        limit = max(1, min(requested_limit, 400)) if requested_limit > 0 else 0
    except ValueError:
        limit = 0
    include_event_ids = bool(limit)
    _, idx = history_page(log_list, len(log_list), limit) if limit else (log_list, 0)
    replay_marked = False
    replayed_request_ids: set[str] = set()
    since_mode = since > 0
    try:
        while not disconnected.is_set():
            if idx < len(log_list):
                start = idx
                batch = log_list[idx:]
                idx = len(log_list)
                stop = False
                for offset, event in enumerate(batch):
                    if event is None:
                        stop = True
                        break
                    seq = event_seq(event) if event_seq(event) is not None else start + offset + 1
                    if since_mode and seq <= since:
                        continue  # 断点之前的事件：客户端已有，跳过（不丢不重）
                    if seq in unknown_seqs and event.get("type") in NO_REPLAY_TYPES:
                        # 结果未知的交互事件不重放（保守投递语义）
                        log.info("跳过结果未知的交互事件（session=%s seq=%d）", sid, seq)
                        unknown_seqs.discard(seq)
                        continue
                    if include_event_ids or since_mode:
                        event = {**event, "event_id": str(seq)}
                    request_id = pending_prompt_request_id(event)
                    if request_id:
                        replayed_request_ids.add(request_id)
                    if event.get("type") in NO_REPLAY_TYPES:
                        server.mark_outbox_pushed(sid, seq)
                    await websocket.send_json(event)
                if stop:
                    break
            else:
                if not replay_marked:
                    replay_marked = True
                    try:
                        replay_data = {"before": idx, "has_more": idx > 0} if limit else {}
                        await websocket.send_json({"type": "ReplayDone", "data": replay_data})
                        pending_events = pending_prompt_events_after_replay(
                            server.pending_prompt_events(sid),
                            replayed_request_ids,
                        )
                        for event in pending_events:
                            await websocket.send_json(event)
                    except Exception:
                        pass
                await asyncio.sleep(0.02)
    except WebSocketDisconnect:
        log.info("WS client disconnected from session %s", sid)
    except Exception:
        log.exception("WS stream error for session %s", sid)
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, Exception):
            pass
        log.info("WS stream ended for session %s", sid)
