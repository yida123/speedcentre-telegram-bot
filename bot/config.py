import os
import re
from dataclasses import dataclass, field

import yaml


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 读取，避免额外依赖。已存在的环境变量优先。"""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int_set(value: str) -> set[int]:
    return {int(x) for x in value.replace(" ", "").split(",") if x}


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_subscriptions(path: str) -> list[tuple[str, str]]:
    """读取本机场固定测速的订阅 [(名称, 订阅链接)]；文件不存在时返回空列表（不启用自动测速）。"""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    subs: list[tuple[str, str]] = []
    for i, item in enumerate(data.get("subscriptions") or [], 1):
        name, url = str((item or {}).get("name") or "").strip(), str((item or {}).get("url") or "").strip()
        if not name or not url:
            raise SystemExit(f"{path} 第 {i} 个订阅缺少 name 或 url")
        if any(name == n for n, _ in subs):
            raise SystemExit(f"{path} 中订阅名「{name}」重复")
        subs.append((name, url))
    return subs


def _share_url(value: str) -> str:
    """空值使用默认分享页地址，off/none/false 表示不创建分享。"""
    value = value.strip()
    if value.lower() in ("off", "none", "false", "0"):
        return ""
    return value or Config.share_url


def _str_list(value: str) -> list[str]:
    return [x for x in value.replace(" ", "").split(",") if x]


def parse_times(value: str) -> list[tuple[int, int]]:
    """解析 "09:00,21:30" 形式的每日时间点。"""
    times = []
    for item in value.replace(" ", "").split(","):
        if not item:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", item)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise SystemExit(f"SCHEDULE_TIMES 中的时间「{item}」格式错误，应为 HH:MM，例如 09:00,21:00")
        times.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(times))


@dataclass
class Config:
    bot_token: str
    api_key: str
    api_base: str = "https://api.speedcentre.plus"
    subscriptions: list[tuple[str, str]] = field(default_factory=list)
    daily_limit: int = 0  # 群成员每人每天测速次数，0 表示不限
    schedule_times: list[tuple[int, int]] = field(default_factory=list)
    auto_chat_ids: set[int] = field(default_factory=set)
    auto_slave_id: str = ""
    data_dir: str = "data"
    timezone: str = "Asia/Shanghai"
    allowed_chat_ids: set[int] = field(default_factory=set)
    admin_user_ids: set[int] = field(default_factory=set)
    allow_private: bool = False
    max_nodes: int = 100
    max_tasks_per_chat: int = 1
    default_slave_id: str = ""
    allowed_backends: set[str] = field(default_factory=set)
    backend_select: bool = True
    sort_select: bool = True
    task_url: str = ""
    # 分享页链接模板，{uuid} 替换为分享 ID（去掉横杠的 32 位十六进制）；off 表示不创建分享
    share_url: str = "https://web.speedcentre.plus/share?share_id={uuid}"
    delete_sub_message: bool = True
    sub_link_pattern: str = ""
    poll_interval: float = 5.0
    task_timeout: float = 1800.0
    # 测速配置（提交任务时的 configs，同 miaospeed 的 SlaveRequestConfigs）。
    # 默认值取自 SpeedCentre+ 官方文档「SpeedCentre+ Copilot - 使用」中的对接示例
    speed_download_url: str = "https://dl.google.com/dl/android/studio/install/3.4.1.0/android-studio-ide-183.5522156-windows.exe"
    speed_duration: int = 8
    speed_threads: int = 4
    ping_url: str = "https://cp.cloudflare.com/generate_204"
    ping_average_over: int = 3
    stun_url: str = "udp://stunserver2025.stunprotocol.org:3478"
    task_retry: int = 3
    dns_servers: list[str] = field(default_factory=list)

    def task_configs(self) -> dict:
        """提交测速任务时的完整 configs。后端当前在省略 configs 时会异常断连，所以每次都带上全部字段。"""
        return {
            "Scripts": [],
            "dnsServers": list(self.dns_servers),
            "downloadDuration": self.speed_duration,
            "downloadThreading": self.speed_threads,
            "downloadURL": self.speed_download_url,
            "pingAddress": self.ping_url,
            "pingAverageOver": self.ping_average_over,
            "stunURL": self.stun_url,
            "taskRetry": self.task_retry,
            "tracerouteMaxHops": 30,
            "tracerouteProbesPerHop": 3,
            "tracerouteTimeout": 0,
        }

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        token = os.environ.get("TG_BOT_TOKEN", "")
        api_key = os.environ.get("SCP_API_KEY", "")
        if not token or not api_key:
            raise SystemExit("请设置环境变量 TG_BOT_TOKEN 和 SCP_API_KEY（可参考 .env.example）")
        return cls(
            bot_token=token,
            api_key=api_key,
            api_base=os.environ.get("SCP_API_BASE", cls.api_base).rstrip("/"),
            subscriptions=load_subscriptions(os.environ.get("SUBSCRIPTIONS_FILE", "subscriptions.yaml")),
            daily_limit=int(os.environ.get("DAILY_LIMIT") or 0),
            # 未设置时默认每天 09:00；显式设为空则关闭自动测速
            schedule_times=parse_times(os.environ.get("SCHEDULE_TIMES", "09:00")),
            auto_chat_ids=_int_set(os.environ.get("AUTO_CHAT_IDS", "")),
            auto_slave_id=os.environ.get("AUTO_SLAVE_ID", ""),
            data_dir=os.environ.get("DATA_DIR", "data"),
            timezone=os.environ.get("TIMEZONE", "Asia/Shanghai"),
            allowed_chat_ids=_int_set(os.environ.get("ALLOWED_CHAT_IDS", "")),
            admin_user_ids=_int_set(os.environ.get("ADMIN_USER_IDS", "")),
            allow_private=_bool(os.environ.get("ALLOW_PRIVATE", "false")),
            max_nodes=int(os.environ.get("MAX_NODES", "100")),
            max_tasks_per_chat=int(os.environ.get("MAX_TASKS_PER_CHAT", "1")),
            default_slave_id=os.environ.get("DEFAULT_SLAVE_ID", ""),
            allowed_backends={x for x in os.environ.get("ALLOWED_BACKENDS", "").replace(" ", "").split(",") if x},
            backend_select=_bool(os.environ.get("BACKEND_SELECT", "true")),
            sort_select=_bool(os.environ.get("SORT_SELECT", "true")),
            task_url=os.environ.get("SCP_TASK_URL", ""),
            share_url=_share_url(os.environ.get("SCP_SHARE_URL", "")),
            delete_sub_message=_bool(os.environ.get("DELETE_SUB_MESSAGE", "true")),
            sub_link_pattern=os.environ.get("SUB_LINK_PATTERN", ""),
            poll_interval=float(os.environ.get("POLL_INTERVAL", "5")),
            task_timeout=float(os.environ.get("TASK_TIMEOUT", "1800")),
            speed_download_url=os.environ.get("SPEED_DOWNLOAD_URL") or cls.speed_download_url,
            speed_duration=int(os.environ.get("SPEED_DURATION") or cls.speed_duration),
            speed_threads=int(os.environ.get("SPEED_THREADS") or cls.speed_threads),
            ping_url=os.environ.get("PING_URL") or cls.ping_url,
            ping_average_over=int(os.environ.get("PING_AVERAGE_OVER") or cls.ping_average_over),
            stun_url=os.environ.get("STUN_URL") or cls.stun_url,
            task_retry=int(os.environ.get("TASK_RETRY") or cls.task_retry),
            dns_servers=_str_list(os.environ.get("DNS_SERVERS", "")),
        )
