import os
from dataclasses import dataclass, field


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


@dataclass
class Config:
    bot_token: str
    api_key: str
    api_base: str = "https://api.speedcentre.plus"
    allowed_chat_ids: set[int] = field(default_factory=set)
    admin_user_ids: set[int] = field(default_factory=set)
    allow_private: bool = False
    max_nodes: int = 100
    max_tasks_per_chat: int = 1
    default_slave_id: str = ""
    allowed_backends: set[str] = field(default_factory=set)
    backend_select: bool = True
    delete_sub_message: bool = True
    sub_link_pattern: str = ""
    dm_target_ttl: float = 1800.0
    poll_interval: float = 5.0
    task_timeout: float = 1800.0

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
            allowed_chat_ids=_int_set(os.environ.get("ALLOWED_CHAT_IDS", "")),
            admin_user_ids=_int_set(os.environ.get("ADMIN_USER_IDS", "")),
            allow_private=_bool(os.environ.get("ALLOW_PRIVATE", "false")),
            max_nodes=int(os.environ.get("MAX_NODES", "100")),
            max_tasks_per_chat=int(os.environ.get("MAX_TASKS_PER_CHAT", "1")),
            default_slave_id=os.environ.get("DEFAULT_SLAVE_ID", ""),
            allowed_backends={x for x in os.environ.get("ALLOWED_BACKENDS", "").replace(" ", "").split(",") if x},
            backend_select=_bool(os.environ.get("BACKEND_SELECT", "true")),
            delete_sub_message=_bool(os.environ.get("DELETE_SUB_MESSAGE", "true")),
            sub_link_pattern=os.environ.get("SUB_LINK_PATTERN", ""),
            poll_interval=float(os.environ.get("POLL_INTERVAL", "5")),
            task_timeout=float(os.environ.get("TASK_TIMEOUT", "1800")),
        )
