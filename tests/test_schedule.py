from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.schedule import ScheduleError, parse_schedule

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")


def nxt(spec, now, tz=SH):
    return parse_schedule(spec, tz).next_after(now, tz)


def test_daily_times():
    s = parse_schedule("21:00, 9:05,21:00")
    assert s.spec == "09:05,21:00" and s.describe() == "每天 09:05、21:00" and not s.is_cron
    assert nxt("09:00,21:00", datetime(2026, 10, 6, 8, 59, tzinfo=SH)) == datetime(2026, 10, 6, 9, 0, tzinfo=SH)
    assert nxt("09:00,21:00", datetime(2026, 10, 6, 9, 0, tzinfo=SH)) == datetime(2026, 10, 6, 21, 0, tzinfo=SH)
    assert nxt("09:00,21:00", datetime(2026, 10, 6, 22, 0, tzinfo=SH)) == datetime(2026, 10, 7, 9, 0, tzinfo=SH)


def test_cron_basics():
    now = datetime(2026, 10, 6, 7, 10, tzinfo=SH)  # 周二
    assert nxt("0 */6 * * *", now) == datetime(2026, 10, 6, 12, 0, tzinfo=SH)
    assert nxt("30 8,20 * * 1-5", now) == datetime(2026, 10, 6, 8, 30, tzinfo=SH)
    assert nxt("0 9 * * mon", now) == datetime(2026, 10, 12, 9, 0, tzinfo=SH)
    assert nxt("0 9 * * 7", now) == datetime(2026, 10, 11, 9, 0, tzinfo=SH)  # 7 也是周日
    assert nxt("@daily", now) == datetime(2026, 10, 7, 0, 0, tzinfo=SH)
    assert nxt("0 0 29 2 *", now) == datetime(2028, 2, 29, 0, 0, tzinfo=SH)
    assert parse_schedule("0 */6 * * *").describe() == "每 6 小时（整点）"
    assert parse_schedule("0 9 * * 1-5").describe() == "按 cron「0 9 * * 1-5」"


def test_dom_and_dow_are_ored():
    # 每月 1 号或每周五（cron 惯例）
    now = datetime(2026, 10, 6, 12, 0, tzinfo=SH)  # 周二
    assert nxt("0 9 1 * fri", now) == datetime(2026, 10, 9, 9, 0, tzinfo=SH)
    assert nxt("0 9 1 * fri", datetime(2026, 10, 30, 12, 0, tzinfo=SH)) == datetime(2026, 11, 1, 9, 0, tzinfo=SH)


def test_dst_fall_back_and_spring_forward():
    # 夏令时结束当天，00:30 EDT 到 09:00 EST 实际经过 9.5 小时
    now = datetime(2026, 11, 1, 0, 30, tzinfo=NY)
    t = nxt("0 9 * * *", now, NY)
    assert (t.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds() == 9.5 * 3600
    # 重复的 01:xx：01:30 EDT 已经跑过，第二个 01:10（EST）不能再触发
    now = datetime(2026, 11, 1, 6, 10, tzinfo=timezone.utc).astimezone(NY)
    t = nxt("30 1 * * *", now, NY)
    assert t.astimezone(timezone.utc) > now.astimezone(timezone.utc)
    assert t.date().day == 2


@pytest.mark.parametrize("spec", ["* * * * *", "*/5 * * * *", "09:00,09:05", "0,5 * * * *"])
def test_too_frequent_is_rejected(spec):
    with pytest.raises(ScheduleError, match="至少间隔"):
        parse_schedule(spec)


@pytest.mark.parametrize("spec", ["25:00", "0 24 * * *", "61 * * * *", "0 9 * *", "0 9 * * abc", "0 9 31 2 *",
                                  "0 9 1-x * *", "0 9 */0 * *"])
def test_invalid_is_rejected(spec):
    with pytest.raises(ScheduleError):
        parse_schedule(spec)


def test_off_values():
    for value in ("", "off", "OFF", "none", None):
        assert parse_schedule(value) is None


def test_interval_shorthand_and_separators():
    assert parse_schedule("6h").spec == "0 */6 * * *"
    assert parse_schedule("每6小时").describe() == "每 6 小时（整点）"
    assert parse_schedule("30m").spec == "*/30 * * * *" and parse_schedule("30m").describe() == "每 30 分钟"
    assert parse_schedule("24h").spec == "0 0 * * *"
    assert parse_schedule("120 min").spec == "0 */2 * * *"
    assert parse_schedule("09:00 21:00").spec == parse_schedule("9:00，21:00").spec == "09:00,21:00"
    for bad in ("90m", "48h", "0h"):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)
    with pytest.raises(ScheduleError, match="至少间隔"):
        parse_schedule("5m")
