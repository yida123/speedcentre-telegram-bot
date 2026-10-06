"""自动测速的时间表：支持 “09:00,21:30” 这样的每日时间点、“6h”“30m” 这样的固定间隔，以及标准 5 段 cron 表达式。

cron 语法：分 时 日 月 周，例如
    0 9 * * *        每天 09:00
    0 */6 * * *      每 6 小时（0、6、12、18 点整）
    30 8,20 * * 1-5  工作日 08:30 和 20:30
    0 9 * * mon      每周一 09:00
支持 * , - / 以及月份、星期的英文缩写（jan…dec、sun…sat），和 @hourly/@daily/@weekly/@monthly/@yearly。
日和周同时指定时按 cron 惯例取“或”。
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# 相邻两次自动测速的最短间隔，防止误写成每分钟一次把积分烧光
MIN_INTERVAL_MINUTES = 10
# 向后最多查找的天数（2 月 29 日这类表达式最多 4 年出现一次）
MAX_LOOKAHEAD_DAYS = 366 * 5

_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
# (名称, 最小值, 最大值, 名称表)
_FIELDS = [("分钟", 0, 59, {}), ("小时", 0, 23, {}), ("日", 1, 31, {}), ("月", 1, 12, _MONTHS), ("星期", 0, 7, _DAYS)]


class ScheduleError(ValueError):
    pass


def _is_number(text: str) -> bool:
    # 只认 ASCII 数字：“²”这类字符 isdigit() 为真，但 int() 会报错
    return text.isascii() and text.isdigit()


def _parse_value(text: str, names: dict, label: str) -> int:
    text = text.lower()
    if text in names:
        return names[text]
    if not _is_number(text):
        raise ScheduleError(f"{label}字段中的「{text}」不是数字")
    return int(text)


def _parse_field(text: str, lo: int, hi: int, names: dict, label: str) -> tuple[frozenset[int], bool]:
    """解析一个 cron 字段，返回 (取值集合, 是否为 *)。"""
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"{label}字段格式错误")
        rng, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not _is_number(step_text) or int(step_text) == 0:
                raise ScheduleError(f"{label}字段的步长「{step_text}」无效")
            step = int(step_text)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = _parse_value(a, names, label), _parse_value(b, names, label)
        else:
            start = _parse_value(rng, names, label)
            end = hi if step_text else start  # “5/15” 表示从 5 开始每 15
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ScheduleError(f"{label}字段的「{part}」超出范围 {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values), text == "*"


@dataclass(frozen=True)
class Schedule:
    spec: str  # 规范化后的原始写法
    times: tuple[tuple[int, int], ...] = ()  # 每日时间点写法
    minutes: frozenset[int] = frozenset()
    hours: frozenset[int] = frozenset()
    doms: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    dows: frozenset[int] = frozenset()  # 0=周日
    dom_any: bool = True
    dow_any: bool = True

    @property
    def is_cron(self) -> bool:
        return not self.times

    def describe(self) -> str:
        if self.times:
            return "每天 " + "、".join(f"{h:02d}:{m:02d}" for h, m in self.times)
        # 能整除一天/一小时的固定间隔用更直观的说法，其余照原样显示 cron
        m = re.fullmatch(r"0 \*/(\d+) \* \* \*", self.spec)
        if m and 24 % int(m.group(1)) == 0:
            return f"每 {int(m.group(1))} 小时（整点）"
        m = re.fullmatch(r"\*/(\d+) \* \* \* \*", self.spec)
        if m and 60 % int(m.group(1)) == 0:
            return f"每 {int(m.group(1))} 分钟"
        return f"按 cron「{self.spec}」"

    def _day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.doms
        dow_ok = (d.weekday() + 1) % 7 in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok  # 日和周都指定时取“或”（cron 惯例）

    def _day_times(self, d: date) -> list[tuple[int, int]]:
        if self.times:
            return list(self.times)
        if not self._day_matches(d):
            return []
        return [(h, m) for h in sorted(self.hours) for m in sorted(self.minutes)]

    def next_after(self, now: datetime, tz: ZoneInfo) -> datetime | None:
        """严格晚于 now 的下一次运行时间（tz 时区的 aware datetime）；找不到时返回 None。
        比较全部换成 UTC：同一 tzinfo 的 aware datetime 比较时会忽略 UTC 偏移，夏令时切换日会出错。"""
        now_local = now.astimezone(tz)
        now_utc = now.astimezone(timezone.utc)
        day = now_local.date()
        for _ in range(MAX_LOOKAHEAD_DAYS):
            best = None
            for h, m in self._day_times(day):
                t = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                if t.astimezone(timezone.utc) > now_utc and (best is None or t.astimezone(timezone.utc) < best[0]):
                    best = (t.astimezone(timezone.utc), t)
            if best:
                return best[1]
            day += timedelta(days=1)
        return None

    def _day_minutes(self) -> list[int]:
        """每个运行日内的运行时刻（从 0 点起的分钟数），升序。"""
        if self.times:
            return sorted(h * 60 + m for h, m in self.times)
        return sorted(h * 60 + m for h in self.hours for m in self.minutes)

    def _runs_on_consecutive_days(self) -> bool:
        """是否存在连续两天都运行（这时要算上从前一天最后一次到第二天第一次的间隔）。"""
        if self.times:
            return True
        day, prev = date(2024, 1, 1), False
        for _ in range(366 * 4 + 1):  # 4 年覆盖所有月份长度和闰年
            cur = self._day_matches(day)
            if cur and prev:
                return True
            day, prev = day + timedelta(days=1), cur
        return False

    def _dst_gaps(self, tz: ZoneInfo) -> list[float]:
        """夏令时切换前后实际（UTC）的运行间隔：例如切换日不存在的 02:50 实际在 03:50 运行。"""
        gaps: list[float] = []
        day = date(2026, 1, 1)
        for _ in range(366 * 2):
            nxt = day + timedelta(days=1)
            if datetime(day.year, day.month, day.day, tzinfo=tz).utcoffset() != \
                    datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).utcoffset():
                start = day - timedelta(days=1)
                end = (datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz) + timedelta(days=2)).astimezone(timezone.utc)
                prev = None
                t = self.next_after(datetime(start.year, start.month, start.day, tzinfo=tz), tz)
                while t is not None and t.astimezone(timezone.utc) < end:
                    if prev is not None:
                        gaps.append((t.astimezone(timezone.utc) - prev.astimezone(timezone.utc)).total_seconds() / 60)
                    prev, t = t, self.next_after(t, tz)
            day = nxt
        return gaps

    def min_gap_minutes(self, tz: ZoneInfo) -> float | None:
        """相邻两次运行的最短间隔（分钟）：同一天内、跨到第二天，以及夏令时切换前后。只运行一次时返回 None。"""
        minutes = self._day_minutes()
        gaps = [float(b - a) for a, b in zip(minutes, minutes[1:])]
        if minutes and self._runs_on_consecutive_days():
            gaps.append(float(minutes[0] + 1440 - minutes[-1]))
        if gaps and min(gaps) < MIN_INTERVAL_MINUTES:
            return min(gaps)  # 已经太密，不必再检查夏令时
        gaps += self._dst_gaps(tz)
        return min(gaps) if gaps else None


def _interval_to_cron(count: int, unit: str) -> str:
    """把“每 N 小时/分钟”换成 cron（从 0 点/整点起算）。"""
    minutes = count * 60 if unit in ("h", "小时", "hour", "hours") else count
    # cron 的 */N 每小时（或每天）从头算起，N 不能整除 60（或 24）时间隔会忽长忽短，所以只接受能整除的
    if 0 < minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0 and 0 < minutes // 60 <= 24 and 24 % (minutes // 60) == 0:
        hours = minutes // 60
        return "0 0 * * *" if hours == 24 else f"0 */{hours} * * *"
    raise ScheduleError("间隔需要能整除 1 小时或 24 小时，例如 10m、15m、20m、30m、1h、2h、3h、4h、6h、8h、12h、24h；"
                        "其他间隔请用 cron 或每日时间点")


def parse_schedule(text: str | None, tz: ZoneInfo | None = None) -> Schedule | None:
    """解析时间表。空、off、none 表示不自动测速（返回 None）。格式错误或间隔过短时抛出 ScheduleError。"""
    text = " ".join((text or "").split())
    if not text or text.lower() in ("off", "none", "false", "0"):
        return None
    lowered = text.lower()
    if lowered in _ALIASES:
        text = _ALIASES[lowered]
    m = re.fullmatch(r"(?:每|every)?\s*([0-9]{1,4})\s*(h|hours?|小时|m|min|mins|minutes?|分钟)", lowered)
    if m:
        text = _interval_to_cron(int(m.group(1)), m.group(2))
    if re.fullmatch(r"[0-9]{1,2}:[0-9]{2}([\s,，、]+[0-9]{1,2}:[0-9]{2})*", text):
        times = set()
        for item in re.split(r"[\s,，、]+", text):
            h, m = (int(x) for x in item.split(":"))
            if h > 23 or m > 59:
                raise ScheduleError(f"时间「{item}」格式错误，应为 HH:MM，例如 09:00,21:00")
            times.add((h, m))
        schedule = Schedule(spec=",".join(f"{h:02d}:{m:02d}" for h, m in sorted(times)), times=tuple(sorted(times)))
    else:
        parts = text.split()
        if len(parts) != 5:
            raise ScheduleError("格式错误：请写每日时间（如 09:00,21:00）或 5 段 cron 表达式（如 0 */6 * * *）")
        parsed = [_parse_field(p, lo, hi, names, label) for p, (label, lo, hi, names) in zip(parts, _FIELDS)]
        dows = frozenset(0 if d == 7 else d for d in parsed[4][0])
        schedule = Schedule(
            spec=" ".join(parts), minutes=parsed[0][0], hours=parsed[1][0], doms=parsed[2][0], months=parsed[3][0],
            dows=dows, dom_any=parsed[2][1], dow_any=parsed[4][1],
        )
    tz = tz or ZoneInfo("Asia/Shanghai")
    if schedule.next_after(datetime(2026, 1, 1, tzinfo=tz), tz) is None:
        raise ScheduleError("这个时间表永远不会触发")
    gap = schedule.min_gap_minutes(tz)
    if gap is not None and gap < MIN_INTERVAL_MINUTES:
        raise ScheduleError(f"两次自动测速至少间隔 {MIN_INTERVAL_MINUTES} 分钟，避免积分很快耗尽")
    return schedule
