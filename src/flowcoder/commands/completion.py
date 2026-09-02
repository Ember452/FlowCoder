"""斜杠命令补全弹层 UI。"""

from __future__ import annotations

from textual.message import Message as TMessage
from textual.widgets import Static


class CompletionPopup(Static):
    DEFAULT_CSS = """
    CompletionPopup {
        height: auto;
        max-height: 15;
        display: none;
        padding: 0 1;
    }
    """

    class Selected(TMessage):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._displays: list[str] = []
        self._values: list[str] = []
        self._cursor: int = 0
        #: 可见窗口行数：列表超出时渲染滚动窗口（光标跟随），而非裁剪
        self._max_visible: int = 12
        self._last_content: str = ""

    def show_pairs(self, pairs: list[tuple[str, str]]) -> None:
        """以 (display_text, value) 对的形式显示候选项。"""
        self._displays = [d for d, _ in pairs]
        self._values = [v for _, v in pairs]
        self._cursor = 0
        self._refresh_content()
        self.display = True

    def show(self, items: list[str]) -> None:
        self.show_pairs([(i, i) for i in items])

    def hide(self) -> None:
        self.display = False
        self._displays = []
        self._values = []
        self._cursor = 0

    @property
    def is_visible(self) -> bool:
        return bool(self.display)

    def move_up(self) -> None:
        if self._displays and self._cursor > 0:
            self._cursor -= 1
            self._refresh_content()

    def move_down(self) -> None:
        if self._displays and self._cursor < len(self._displays) - 1:
            self._cursor += 1
            self._refresh_content()

    def get_selected(self) -> str | None:
        if not self._values:
            return None
        return self._values[self._cursor]

    def _refresh_content(self) -> None:
        """按光标位置渲染候选项列表，高亮当前项并处理滚动窗口。"""
        total = len(self._displays)
        if total == 0:
            self._last_content = ""
            self.update("")
            return
        visible = min(self._max_visible, total)
        # 滚动窗口：光标移动到窗口边缘时视口跟随（经典 listbox 行为），
        # 保证任何一项都可达——Static 不可滚动，靠窗口渲染实现
        start = 0
        if total > visible:
            start = min(max(0, self._cursor - visible + 1), total - visible)
            if self._cursor < start:
                start = self._cursor
        lines: list[str] = []
        if start > 0:
            lines.append(f"  [dim]▲ {start} 项[/]")
        for i in range(start, min(start + visible, total)):
            display = self._displays[i]
            if i == self._cursor:
                lines.append(f"[bold reverse] {display} [/]")
            else:
                lines.append(f"  [dim]{display}[/]")
        if start + visible < total:
            lines.append(f"  [dim]▼ 还有 {total - start - visible} 项[/]")
        self._last_content = "\n".join(lines)
        self.update(self._last_content)

    def on_click(self) -> None:
        selected = self.get_selected()
        if selected:
            self.post_message(self.Selected(selected))
            self.hide()
