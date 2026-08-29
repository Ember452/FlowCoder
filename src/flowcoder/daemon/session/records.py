"""内存会话记录 + 持久化元数据/事件。"""

from __future__ import annotations

import logging
import time

from flowcoder.daemon.session.meta import (
    new_session_meta,
    session_info_from_meta,
    session_work_dir_from_meta,
    sort_session_ids_by_created_at,
)
from flowcoder.daemon.session.store import SessionStore
from flowcoder.conversation import ConversationManager
from flowcoder.daemon.session.conversation_snapshot import serialize_conversation

log = logging.getLogger(__name__)


class SessionRecords:
    """In-memory session records with durable metadata/event persistence."""

    def __init__(self, store: SessionStore, server_work_dir: str) -> None:
        self.store = store
        self.server_work_dir = server_work_dir
        self.event_logs: dict[str, list[dict | None]] = {}
        self.session_meta: dict[str, dict] = {}
        self.persisted_count: dict[str, int] = {}
        #: Outbox（P5c）：会话内单调事件序号；ack 后写进 meta 持久化
        self.ack_seq: dict[str, int] = {}

    def load_persisted(self) -> int:
        """Load persisted sessions from disk; runtimes are created lazily."""
        for session in self.store.load_sessions():
            self.event_logs[session.sid] = session.events
            self.session_meta[session.sid] = session.meta
            self.persisted_count[session.sid] = len(session.events)
            ack = session.meta.get("outbox_ack_seq")
            if isinstance(ack, int):
                self.ack_seq[session.sid] = ack
        if self.session_meta:
            log.info("Loaded %d persisted session(s)", len(self.session_meta))
        return len(self.session_meta)

    def create(self, sid: str, work_dir: str, provider_name: str = "") -> None:
        self.event_logs[sid] = []
        self.session_meta[sid] = new_session_meta(work_dir, provider_name=provider_name)
        self.persisted_count[sid] = 0
        self.persist_meta(sid)

    def has(self, sid: str) -> bool:
        return sid in self.event_logs

    def ensure_event_log(self, sid: str) -> None:
        self.event_logs.setdefault(sid, [])

    def event_log(self, sid: str) -> list[dict | None] | None:
        return self.event_logs.get(sid)

    def emit(self, sid: str, event: dict | None) -> None:
        log_list = self.event_logs.get(sid)
        if log_list is not None:
            if event is not None:
                # Outbox 盖章（P5c）：seq 单调、ts 用于保留期清理。
                # seq 取"历史最大 seq + 1"而非 len+1：保留期清理会缩短
                # 磁盘/内存视图，len 不再是可靠的单调来源
                event.setdefault("seq", self._next_seq(sid))
                event.setdefault("ts", time.time())
            log_list.append(event)

    def _next_seq(self, sid: str) -> int:
        highest = self.ack_seq.get(sid, 0)
        for item in self.event_logs.get(sid, []):
            if item is None:
                continue
            seq = item.get("seq")
            if isinstance(seq, int) and seq > highest:
                highest = seq
        return highest + 1

    def set_ack(self, sid: str, seq: int) -> None:
        """客户端确认已收到 seq（单调取大），持久化进 session meta。"""
        current = self.ack_seq.get(sid, 0)
        if seq > current:
            self.ack_seq[sid] = seq
            self.session_meta.setdefault(sid, {})["outbox_ack_seq"] = seq
            self.persist_meta(sid)

    def get_ack(self, sid: str) -> int:
        return self.ack_seq.get(sid, 0)

    def persist_meta(self, sid: str) -> None:
        self.store.persist_meta(sid, self.session_meta.get(sid, {}))

    def persist_events(self, sid: str) -> None:
        log_list = self.event_logs.get(sid)
        if log_list is None:
            return
        self.persisted_count[sid] = self.store.persist_events(
            sid,
            log_list,
            self.persisted_count.get(sid, 0),
        )

    def persist_conversation(self, sid: str, conversation: ConversationManager) -> None:
        meta = self.session_meta.get(sid)
        if meta is None or not isinstance(conversation, ConversationManager):
            return
        meta["conversation"] = serialize_conversation(conversation)
        self.persist_meta(sid)

    def set_title_from_prompt(self, sid: str, prompt: str) -> None:
        meta = self.session_meta.get(sid)
        if meta is not None and not meta.get("title"):
            meta["title"] = prompt[:40]
            self.persist_meta(sid)

    def info(self, sid: str) -> dict:
        return session_info_from_meta(
            sid,
            self.session_meta.get(sid),
            self.server_work_dir,
        )

    def list_infos(self) -> list[dict]:
        sids = sort_session_ids_by_created_at(
            self.event_logs.keys(),
            self.session_meta,
        )
        return [self.info(sid) for sid in sids]

    def work_dir(self, sid: str) -> str | None:
        meta = self.session_meta.get(sid)
        if meta is None:
            return None
        return session_work_dir_from_meta(meta, self.server_work_dir)

    def update_work_dir(self, sid: str, work_dir: str) -> None:
        self.session_meta.setdefault(sid, {})["work_dir"] = work_dir
        self.persist_meta(sid)

    def meta(self, sid: str) -> dict:
        return self.session_meta.get(sid, {})

    def close(self, sid: str) -> None:
        log_list = self.event_logs.pop(sid, None)
        if log_list is not None:
            log_list.append(None)
        self.session_meta.pop(sid, None)
        self.persisted_count.pop(sid, None)
        self.store.delete_session(sid)
