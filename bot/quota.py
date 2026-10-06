"""每人每日测试次数限制，持久化到 JSON 文件，重启不清零。"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("speed_bot")


class DailyQuota:
    def __init__(self, path: str, limit: int, timezone: str = "Asia/Shanghai"):
        self.path = path
        self.limit = limit  # <= 0 表示不限制
        self.tz = ZoneInfo(timezone)
        self.date = ""
        self.counts: dict[str, int] = {}
        self._load()

    def _today(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.date, self.counts = data.get("date", ""), {str(k): int(v) for k, v in data.get("counts", {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("读取次数记录 %s 失败，将重新计数：%s", self.path, e)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"date": self.date, "counts": self.counts}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("保存次数记录 %s 失败：%s", self.path, e)

    def _roll(self) -> None:
        today = self._today()
        if self.date != today:
            self.date, self.counts = today, {}

    def used(self, user_id: int) -> int:
        self._roll()
        return self.counts.get(str(user_id), 0)

    def remaining(self, user_id: int) -> int | None:
        """今日剩余次数；不限制时返回 None。"""
        if self.limit <= 0:
            return None
        return max(0, self.limit - self.used(user_id))

    def consume(self, user_id: int) -> int | None:
        """记一次并返回剩余次数。"""
        if self.limit <= 0:
            return None
        self._roll()
        key = str(user_id)
        self.counts[key] = self.counts.get(key, 0) + 1
        self._save()
        return max(0, self.limit - self.counts[key])

    def refund(self, user_id: int) -> None:
        """退回一次（提交失败时）。"""
        if self.limit <= 0:
            return
        self._roll()
        key = str(user_id)
        if self.counts.get(key, 0) > 0:
            self.counts[key] -= 1
            self._save()
