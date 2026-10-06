import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import yaml

from .schedule import ScheduleError, parse_schedule


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


# 网页分享页地址模板：设置 SCP_SHARE_URL 为它即可在结果图下附上「查看详情」分享链接
WEB_SHARE_URL = "https://web.speedcentre.plus/share?share_id={uuid}"


def _share_url(value: str) -> str:
    """默认不创建分享；填写链接模板（如 WEB_SHARE_URL）才开启，off/none/false 同样表示关闭。"""
    value = value.strip()
    if value.lower() in ("off", "none", "false", "0"):
        return ""
    return value


def _str_list(value: str) -> list[str]:
    return [x for x in value.replace(" ", "").split(",") if x]


def _schedule_spec(value: str, tz: str) -> str:
    """校验 SCHEDULE_TIMES（每日时间点或 cron），返回规范化写法；空值表示不自动测速。"""
    try:
        schedule = parse_schedule(value, ZoneInfo(tz))
    except ScheduleError as e:
        raise SystemExit(f"SCHEDULE_TIMES 无效：{e}") from e
    return schedule.spec if schedule else ""


@dataclass
class Config:
    bot_token: str
    api_key: str
    api_base: str = "https://api.speedcentre.plus"
    subscriptions: list[tuple[str, str]] = field(default_factory=list)
    daily_limit: int = 0  # 群成员每人每天测速次数，0 表示不限
    # 自动测速时间表：每日时间点（09:00,21:00）或 5 段 cron；空表示不自动测速
    schedule_spec: str = ""
    auto_chat_ids: set[int] = field(default_factory=set)
    auto_slave_id: str = ""
    data_dir: str = "data"
    timezone: str = "Asia/Shanghai"
    allowed_chat_ids: set[int] = field(default_factory=set)
    admin_user_ids: set[int] = field(default_factory=set)  # .env 中的超级管理员，命令不能移除
    extra_admin_ids: set[int] = field(default_factory=set)  # 超级管理员在私聊里添加的管理员
    banned_user_ids: set[int] = field(default_factory=set)  # 被禁止测速的用户
    cooldown_seconds: int = 0  # 群成员两次测速的最短间隔，0 表示不限
    member_max_nodes: int = 0  # 群成员单次最多测多少个节点，0 表示使用 MAX_NODES
    credit_alert: int = 0  # 当天积分消耗超过该值时私聊提醒管理员，0 表示不提醒
    pin_auto_result: bool = True  # 置顶最新的本机场测速结果
    anomaly_percent: int = 50  # 本机场测速中异常节点占比达到该百分比时提醒管理员，0 表示不提醒
    allow_private: bool = False
    max_nodes: int = 100
    max_tasks_per_chat: int = 1
    default_slave_id: str = ""
    allowed_backends: set[str] = field(default_factory=set)
    backend_select: bool = True
    sort_select: bool = True
    task_url: str = ""
    # 分享页链接模板，{uuid} 替换为分享 ID（去掉横杠的 32 位十六进制）；默认空，不创建分享、不附链接
    share_url: str = ""
    delete_sub_message: bool = True
    sub_link_pattern: str = ""
    poll_interval: float = 5.0
    # 群里除测速结果外的消息（提示、菜单、进度、用户的命令）多少秒后删除；0 表示不删
    auto_delete_seconds: float = 10.0
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
            schedule_spec=_schedule_spec(os.environ.get("SCHEDULE_TIMES", "09:00"),
                                         os.environ.get("TIMEZONE", "Asia/Shanghai")),
            auto_chat_ids=_int_set(os.environ.get("AUTO_CHAT_IDS", "")),
            auto_slave_id=os.environ.get("AUTO_SLAVE_ID", ""),
            data_dir=os.environ.get("DATA_DIR", "data"),
            timezone=os.environ.get("TIMEZONE", "Asia/Shanghai"),
            allowed_chat_ids=_int_set(os.environ.get("ALLOWED_CHAT_IDS", "")),
            admin_user_ids=_int_set(os.environ.get("ADMIN_USER_IDS", "")),
            allow_private=_bool(os.environ.get("ALLOW_PRIVATE", "false")),
            cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS") or 0),
            member_max_nodes=int(os.environ.get("MEMBER_MAX_NODES") or 0),
            credit_alert=int(os.environ.get("CREDIT_ALERT") or 0),
            pin_auto_result=_bool(os.environ.get("PIN_AUTO_RESULT") or "true"),
            anomaly_percent=int(os.environ.get("ANOMALY_ALERT_PERCENT") or cls.anomaly_percent),
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
            auto_delete_seconds=float(os.environ.get("AUTO_DELETE_SECONDS") or cls.auto_delete_seconds),
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
