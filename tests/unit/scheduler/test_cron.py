"""cron 表达式解析与 next_after 边界测试（P5a）。"""

from __future__ import annotations

import datetime as dt

import pytest

from flowcoder.core.cron import CronError, CronExpr


def _dt(y, mo, d, h=0, mi=0, s=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, s)


class TestParse:
    def test_valid_variants(self) -> None:
        for expr in [
            "* * * * *",
            "0 0 * * *",
            "*/5 * * * *",
            "0 9 * * 1-5",
            "30 8 1,15 * *",
            "0 0 29 2 *",
            "5 4 * * 0",
            "5 4 * * 7",  # 7 亦为周日
            "10-20/3 * * * *",
        ]:
            CronExpr.parse(expr)

    @pytest.mark.parametrize(
        "bad",
        [
            "* * * *",  # 字段数不足
            "* * * * * *",  # 字段数超
            "60 * * * *",  # 分钟越界
            "* 24 * * *",  # 小时越界
            "* * 0 * *",  # 日从 1 起
            "* * * 13 *",  # 月越界
            "a * * * *",  # 非数字
            "*/0 * * * *",  # 步进 0
            "10-5 * * * *",  # 区间倒置
            "JAN * * * *",  # 不支持英文名，明确报错
            "",  # 空
        ],
    )
    def test_invalid_rejected(self, bad: str) -> None:
        with pytest.raises(CronError):
            CronExpr.parse(bad)


class TestNextAfter:
    def test_every_minute(self) -> None:
        c = CronExpr.parse("* * * * *")
        assert c.next_after(_dt(2026, 8, 29, 10, 3, 17)) == _dt(2026, 8, 29, 10, 4)

    def test_step_five(self) -> None:
        c = CronExpr.parse("*/5 * * * *")
        assert c.next_after(_dt(2026, 8, 29, 10, 3)) == _dt(2026, 8, 29, 10, 5)
        assert c.next_after(_dt(2026, 8, 29, 10, 5)) == _dt(2026, 8, 29, 10, 10)

    def test_strictly_after(self) -> None:
        # 整点触发时刻的 next_after 必须是下一分钟，而非原地
        c = CronExpr.parse("* * * * *")
        assert c.next_after(_dt(2026, 8, 29, 10, 0)) == _dt(2026, 8, 29, 10, 1)

    def test_year_rollover(self) -> None:
        c = CronExpr.parse("0 0 1 1 *")
        assert c.next_after(_dt(2026, 12, 31, 23, 59)) == _dt(2027, 1, 1)

    def test_leap_day(self) -> None:
        c = CronExpr.parse("0 0 29 2 *")
        assert c.next_after(_dt(2026, 3, 1)) == _dt(2028, 2, 29)  # 2028 是闰年

    def test_month_skip(self) -> None:
        c = CronExpr.parse("0 0 1 3 *")  # 每年 3 月 1 日
        assert c.next_after(_dt(2026, 4, 1)) == _dt(2027, 3, 1)

    def test_hour_jump(self) -> None:
        c = CronExpr.parse("0 9 * * *")  # 每天 09:00
        assert c.next_after(_dt(2026, 8, 29, 10, 0)) == _dt(2026, 8, 30, 9, 0)

    def test_weekday_only(self) -> None:
        c = CronExpr.parse("0 9 * * 1-5")  # 工作日
        # 2026-08-29 是周六 → 下一个是周一 08-31
        assert c.next_after(_dt(2026, 8, 29, 9, 0)) == _dt(2026, 8, 31, 9, 0)

    def test_sunday_alias_seven(self) -> None:
        c0 = CronExpr.parse("0 9 * * 0")
        c7 = CronExpr.parse("0 9 * * 7")
        # 2026-08-30 是周日
        assert c0.next_after(_dt(2026, 8, 29)) == _dt(2026, 8, 30, 9, 0)
        assert c7.next_after(_dt(2026, 8, 29)) == _dt(2026, 8, 30, 9, 0)

    def test_dom_dow_or_semantics(self) -> None:
        # 日与周都受限：标准 cron 取 OR——13 号是周五 或 周五都触发
        c = CronExpr.parse("0 0 13 * 5")  # 13 号 或 周五
        # 2026-09-01 是周二；最近匹配：09-04（周五）先于 09-13
        assert c.next_after(_dt(2026, 9, 1)) == _dt(2026, 9, 4)
        # 2026-09-13 是周日：dom 命中 → 触发（OR 语义）
        assert c.next_after(_dt(2026, 9, 12)) == _dt(2026, 9, 13)

    def test_dom_only_restricted(self) -> None:
        c = CronExpr.parse("0 0 13 * *")
        assert c.next_after(_dt(2026, 9, 14)) == _dt(2026, 10, 13)

    def test_next_fire_count_between(self) -> None:
        c = CronExpr.parse("*/5 * * * *")
        # (10:00, 10:30) 起点排他：05,10,15,20,25 共 5 次（00 与 30 均不含）
        start = _dt(2026, 8, 29, 10, 0)
        end = _dt(2026, 8, 29, 10, 30)
        assert c.next_fire_count_between(start, end) == 5

    def test_no_fire_within_deadline(self) -> None:
        c = CronExpr.parse("0 0 30 2 *")  # 2 月 30 日：不存在
        with pytest.raises(CronError):
            c.next_after(_dt(2026, 1, 1))
