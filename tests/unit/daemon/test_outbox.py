"""Outbox 核心（P5c）：ledger、保守重放判定、保留期清理。"""

from __future__ import annotations

import json
from pathlib import Path

from flowcoder.daemon.outbox import (
    NO_REPLAY_TYPES,
    OutboxLedger,
    cleanup_outbox_file,
    event_seq,
    should_replay,
)


class TestLedger:
    def test_mark_take_ack(self) -> None:
        ledger = OutboxLedger()
        ledger.mark_pushed("s1", 3)
        ledger.mark_pushed("s1", 5)
        ledger.mark_pushed("s2", 7)
        assert ledger.take_unknown("s1") == {3, 5}
        assert ledger.take_unknown("s1") == set()  # 取走即清空（已知跳过）
        assert ledger.take_unknown("s2") == {7}

    def test_ack_removes_covered(self) -> None:
        ledger = OutboxLedger()
        ledger.mark_pushed("s1", 3)
        ledger.mark_pushed("s1", 5)
        ledger.ack("s1", 4)
        assert ledger.take_unknown("s1") == {5}  # 3 已确认，5 仍未知


class TestShouldReplay:
    def test_interactive_unknown_not_replayed(self) -> None:
        event = {"type": "PermissionRequest", "seq": 4}
        assert event["type"] in NO_REPLAY_TYPES
        assert not should_replay(event, unknown_seqs={4})
        assert should_replay(event, unknown_seqs=set())  # 无未知 → 重放

    def test_normal_events_replay_even_if_unknown(self) -> None:
        # 渲染类事件幂等：已推未 ack 也重放
        assert should_replay({"type": "StreamText", "seq": 4}, unknown_seqs={4})

    def test_no_seq_always_replays(self) -> None:
        assert should_replay({"type": "PermissionRequest"}, unknown_seqs={1, 2})


class TestEventSeq:
    def test_extract_and_validate(self) -> None:
        assert event_seq({"seq": 3}) == 3
        assert event_seq({}) is None
        assert event_seq({"seq": "3"}) is None
        assert event_seq({"seq": True}) is None  # bool 不算


class TestCleanup:
    def _write(self, path: Path, events: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def _read(self, path: Path) -> list[dict]:
        return [
            json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]

    def test_drops_old_acked_keeps_unacked(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        self._write(
            path,
            [
                {"type": "A", "seq": 1, "ts": 1000.0},  # 旧 + 已 ack → 删
                {"type": "B", "seq": 2, "ts": 1000.0},  # 旧但未 ack → 留（不误删未投递）
                {"type": "C", "seq": 3, "ts": 9000.0},  # 新 → 留
            ],
        )
        kept, dropped = cleanup_outbox_file(path, now=10_000.0, retention_s=5_000.0, acked_seq=1)
        assert dropped == 1 and kept == 2
        remaining = self._read(path)
        assert [e["seq"] for e in remaining] == [2, 3]

    def test_legacy_lines_without_stamps_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        self._write(path, [{"type": "Legacy"}])
        kept, dropped = cleanup_outbox_file(path, now=10_000.0, retention_s=1.0, acked_seq=999)
        assert dropped == 0 and kept == 1

    def test_no_drop_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        original = '{"type": "A", "seq": 1, "ts": 1000.0}\n'
        path.write_text(original, encoding="utf-8")
        cleanup_outbox_file(path, now=10_000.0, retention_s=5_000.0, acked_seq=0)
        assert path.read_text(encoding="utf-8") == original  # 无删除不重写

    def test_missing_file(self, tmp_path: Path) -> None:
        kept, dropped = cleanup_outbox_file(
            tmp_path / "nope.jsonl", now=1.0, retention_s=1.0, acked_seq=0
        )
        assert (kept, dropped) == (0, 0)
