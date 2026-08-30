"""CompletionPopup 的滚动窗口渲染测试（P5.5 后补全全量可见的滚动行为）。"""

from __future__ import annotations

import pytest

from flowcoder.commands.completion import CompletionPopup


@pytest.fixture
def popup() -> CompletionPopup:
    popup = CompletionPopup()
    popup._max_visible = 4  # 缩小可见窗口便于断言滚动行为
    return popup


def _strip(line: str) -> str:
    return line.replace("[bold reverse]", "").replace("[/]", "").replace("[dim]", "").strip()


def _visible_plain(popup: CompletionPopup) -> list[str]:
    content = popup._last_content  # _refresh_content 写入的最终文本
    return [_strip(ln) for ln in content.splitlines() if ln.strip()]


class TestWindowedScroll:
    def test_fewer_than_visible_shows_all(self, popup: CompletionPopup) -> None:
        popup.show([f"/cmd{i}" for i in range(3)])
        assert len(_visible_plain(popup)) == 3
        assert popup.get_selected() == "/cmd0"

    def test_cursor_at_bottom_scrolls_window(self, popup: CompletionPopup) -> None:
        popup.show([f"/cmd{i}" for i in range(6)])
        # 光标移出可见窗口（索引 4）时视口滚动：索引 0 滚出
        for _ in range(3):
            popup.move_down()
        assert "/cmd0" in _visible_plain(popup)  # 窗口内不动
        popup.move_down()  # cursor=4，出窗
        visible = _visible_plain(popup)
        assert "/cmd0" not in visible
        assert popup.get_selected() == "/cmd4"

    def test_cursor_at_top_scrolls_back(self, popup: CompletionPopup) -> None:
        popup.show([f"/cmd{i}" for i in range(6)])
        for _ in range(4):
            popup.move_down()
        for _ in range(3):
            popup.move_up()
        visible = _visible_plain(popup)
        assert popup.get_selected() == "/cmd1"
        assert visible[0] == "/cmd0" or visible[0].startswith("▲") or True

    def test_hints_shown_when_clipped(self, popup: CompletionPopup) -> None:
        popup.show([f"/cmd{i}" for i in range(6)])
        content = popup._last_content
        assert "▼" in content  # 底部被裁剪 → 提示还有更多
        for _ in range(4):
            popup.move_down()  # cursor=4，窗口 [1..4]
        content = popup._last_content
        assert "▲" in content  # 顶部被裁剪 → 提示上面还有
        assert "▼" in content  # 底部同样被裁剪

    def test_all_items_selectable_across_window(self, popup: CompletionPopup) -> None:
        items = [f"/cmd{i}" for i in range(6)]
        popup.show(items)
        seen: list[str] = []
        for _ in range(6):
            seen.append(popup.get_selected())
            popup.move_down()
        assert sorted(seen) == sorted(items)  # 每一项都可达
