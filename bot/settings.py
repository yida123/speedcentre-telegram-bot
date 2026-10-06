"""管理员在私聊里修改的设置，以及每日统计。保存在 DATA_DIR 下的 JSON 文件，重启不丢。

settings.json 中出现的项会覆盖 .env 里的同名配置；没出现的项继续用 .env。
"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("speed_bot")


def _write_json(path: str, data: dict) -> None:
    """原子写入，并只允许当前用户读写（设置里有订阅地址）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path: str, strict: bool = False) -> dict:
    """读取 JSON 对象；文件不存在时返回 {}。读取或解析失败时，strict=True 直接退出，否则记录警告并返回 {}。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是 JSON 对象")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if strict:
            # 不能带着空设置继续运行：授权群会变成“不限”，下一次保存还会把整个文件覆盖掉
            raise SystemExit(f"无法读取设置文件 {path}：{e}。请修复（保证是合法的 JSON）或删除这个文件后重启") from e
        log.warning("读取 %s 失败，忽略其中的数据：%s", path, e)
        return {}


class SettingsStore:
    """可在运行时修改的设置。键与 Config 字段同名；值为 None 或缺失表示使用 .env。"""

    # 键 -> 从 JSON 值转换成 Config 字段值的函数
    FIELDS = {
        "allowed_chat_ids": lambda v: {int(x) for x in v},
        "extra_admin_ids": lambda v: {int(x) for x in v},
        "banned_user_ids": lambda v: {int(x) for x in v},
        "subscriptions": lambda v: [(str(n), str(u)) for n, u in v],
        "schedule_spec": str,
        "cooldown_seconds": int,
        "daily_limit": int,
        "member_max_nodes": int,
        "credit_alert": int,
        "pin_auto_result": bool,
        "anomaly_percent": int,
    }

    def __init__(self, path: str):
        self.path = path
        self.data = _read_json(path, strict=True)

    def apply_to(self, cfg) -> None:
        """把已保存的设置套到 Config 上（启动时调用一次）。"""
        for key, convert in self.FIELDS.items():
            if self.data.get(key) is None:
                continue
            try:
                setattr(cfg, key, convert(self.data[key]))
            except (TypeError, ValueError) as e:
                log.warning("设置项 %s 的值无效，已忽略：%s", key, e)

    def set(self, key: str, value) -> None:
        """保存一项设置（集合按排序后的列表保存）。"""
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        elif key == "subscriptions" and value is not None:
            value = [[n, u] for n, u in value]
        self.data[key] = value
        _write_json(self.path, self.data)

    # 置顶的结果消息（不是配置，是运行状态）：chat_id -> [message_id, ...]
    def pinned(self, chat_id: int) -> list[int]:
        return [int(x) for x in self.data.get("pinned", {}).get(str(chat_id), [])]

    def set_pinned(self, chat_id: int, message_ids: list[int]) -> None:
        self.data.setdefault("pinned", {})[str(chat_id)] = list(message_ids)
        _write_json(self.path, self.data)


class DailyStats:
    """当天的测速次数和积分消耗（按 TIMEZONE 每天 0 点重置）。"""

    def __init__(self, path: str, timezone: str = "Asia/Shanghai"):
        self.path = path
        self.tz = ZoneInfo(timezone)
        data = _read_json(path)
        self.date = data.get("date", "")
        self.tests = int(data.get("tests", 0))
        self.credits = int(data.get("credits", 0))
        self.alerted = bool(data.get("alerted", False))

    def _roll(self) -> None:
        today = datetime.now(self.tz).date().isoformat()
        if self.date != today:
            self.date, self.tests, self.credits, self.alerted = today, 0, 0, False

    def _save(self) -> None:
        try:
            _write_json(self.path, {"date": self.date, "tests": self.tests, "credits": self.credits,
                                    "alerted": self.alerted})
        except OSError as e:
            log.warning("保存统计 %s 失败：%s", self.path, e)

    def record(self, credits: int) -> None:
        self._roll()
        self.tests += 1
        self.credits += max(0, int(credits or 0))
        self._save()

    def today(self) -> tuple[int, int]:
        self._roll()
        return self.tests, self.credits

    def should_alert(self, threshold: int) -> bool:
        """当天积分首次超过阈值时返回 True（每天只提醒一次）。"""
        self._roll()
        if threshold <= 0 or self.alerted or self.credits < threshold:
            return False
        self.alerted = True
        self._save()
        return True
