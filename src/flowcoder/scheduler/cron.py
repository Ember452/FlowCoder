"""5 字段 cron 表达式解析与下一次触发时间计算（自实现，P5a）。

选型说明见 docs/specs P5a ADR：不引入 APScheduler——调度器只需要
"解析 + next_after"两个纯函数，自实现约 150 行、零依赖、可全边界测试；
APScheduler 带来 executor/事件循环绑定等与本项目异步生成器风格冲突的抽象。

语法：`分 时 日 月 周`，支持 `*`、数字、列表 `a,b`、区间 `a-b`、步进
`*/n` 与 `a-b/n`。周字段 0=周日（7 亦视为周日）。不支持月份/星期英文名
（如 JAN/MON）——守护任务用不到，解析失败要明确报错而不是猜。
日/周字段遵循标准 cron 语义：两者都受限时取 OR，其一为 `*` 时取另一者。
"""

from __future__ import annotations

import datetime as dt

MINUTES, HOURS, DAYS, MONTHS, WEEKDAYS = range(5)

_FIELD_RANGES = {MINUTES: (0, 59), HOURS: (0, 23), DAYS: (1, 31), MONTHS: (1, 12), WEEKDAYS: (0, 7)}

_FIELD_NAMES = {MINUTES: "分钟", HOURS: "小时", DAYS: "日", MONTHS: "月", WEEKDAYS: "周"}


class CronError(ValueError):
    """cron 表达式非法。"""


def _parse_value(token: str, field: int) -> int:
    lo, hi = _FIELD_RANGES[field]
    try:
        value = int(token)
    except ValueError as e:
        raise CronError(f"{_FIELD_NAMES[field]}字段含非数字值: {token!r}") from e
    if field == WEEKDAYS and value == 7:
        value = 0  # 7 亦视为周日
    if not lo <= value <= hi:
        raise CronError(f"{_FIELD_NAMES[field]}字段超出范围 [{lo},{hi}]: {value}")
    return value


def _parse_field(spec: str, field: int) -> frozenset[int]:
    """解析单个字段为允许值集合。"""
    lo, _hi = _FIELD_RANGES[field]
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        body = part
        if "/" in part:
            body, step_str = part.split("/", maxsplit=1)
            try:
                step = int(step_str)
            except ValueError as e:
                raise CronError(f"{_FIELD_NAMES[field]}字段步进非法: {part!r}") from e
            if step <= 0:
                raise CronError(f"{_FIELD_NAMES[field]}字段步进必须为正数: {part!r}")
        if body == "*":
            start, end = lo, _FIELD_RANGES[field][1]
        elif "-" in body:
            a, b = body.split("-", maxsplit=1)
            start, end = _parse_value(a, field), _parse_value(b, field)
            if start > end:
                raise CronError(f"{_FIELD_NAMES[field]}字段区间起止倒置: {part!r}")
        else:
            start = end = _parse_value(body, field)
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"{_FIELD_NAMES[field]}字段为空: {spec!r}")
    return frozenset(values)


class CronExpr:
    """解析后的 cron 表达式；next_after 计算严格晚于给定时刻的下一次触发。"""

    def __init__(self, expression: str) -> None:
        parts = expression.split()
        if len(parts) != 5:
            raise CronError(f"cron 表达式必须为 5 个字段（分 时 日 月 周）: {expression!r}")
        self.expression = expression
        self.minutes = _parse_field(parts[0], MINUTES)
        self.hours = _parse_field(parts[1], HOURS)
        self.days = _parse_field(parts[2], DAYS)
        self.months = _parse_field(parts[3], MONTHS)
        self.weekdays = _parse_field(parts[4], WEEKDAYS)
        # 标准 cron 语义：日/周均受限时取 OR；记录两者是否受限
        self._dom_restricted = parts[2] != "*"
        self._dow_restricted = parts[4] != "*"

    @classmethod
    def parse(cls, expression: str) -> CronExpr:
        return cls(expression)

    def _day_matches(self, candidate: dt.datetime) -> bool:
        dom_ok = candidate.day in self.days
        # Python weekday(): Mon=0..Sun=6；cron 0=Sun。映射：cron_wd = (py_wd + 1) % 7
        cron_wd = (candidate.weekday() + 1) % 7
        dow_ok = cron_wd in self.weekdays
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True

    def next_after(self, after: dt.datetime) -> dt.datetime:
        """严格晚于 after 的下一次触发时刻（ naive 本地时间，分钟精度）。"""
        candidate = (after + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
        # 最坏扫描上界：4 年（覆盖闰年周期），防死循环
        deadline = after + dt.timedelta(days=366 * 4)
        while candidate <= deadline:
            if candidate.month not in self.months:
                # 跳到下月 1 日 00:00
                year, month = candidate.year, candidate.month + 1
                if month > 12:
                    year, month = year + 1, 1
                candidate = candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_matches(candidate):
                candidate = (candidate + dt.timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = (candidate + dt.timedelta(hours=1)).replace(minute=0)
                continue
            if candidate.minute not in self.minutes:
                candidate += dt.timedelta(minutes=1)
                continue
            return candidate
        raise CronError(f"cron 表达式 {self.expression!r} 在 4 年内无触发时刻")

    def next_fire_count_between(self, start: dt.datetime, end: dt.datetime) -> int:
        """[start, end) 区间内的触发次数（防抖合并时统计错过的窗口数）。"""
        count = 0
        cursor = start
        while True:
            nxt = self.next_after(cursor)
            if nxt >= end:
                return count
            count += 1
            cursor = nxt

    def __str__(self) -> str:
        return self.expression
