"""SpeedCentre+ Telegram 群组测速 Bot。

- 按时间表（每日时间点、固定间隔或 cron）自动测速本机场的固定订阅，结果发到群里并置顶；管理员可随时手动触发。
- 群成员可以私聊发送任意订阅链接测速，结果发回群里并 @ 发起人。
- 管理员在私聊里管理授权群、管理员、封禁、本机场订阅、时间表和各项限制（见 admin.py）。
"""
import asyncio
import io
import logging
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
    filters,
)

from .admin import ADMIN_COMMANDS, USER_COMMANDS, AdminCommands
from .api import APIError, SCPClient
from .config import Config
from .formatter import (
    PRESETS, TEST_OPTIONS, TestPlan, build_plan, esc, fmt_duration, format_result_text, format_stats, node_speed, progress_bar,
)
from .quota import DailyQuota
from .schedule import Schedule, ScheduleError, parse_schedule
from .settings import DailyStats, SettingsStore
from .subscription import (
    DEFAULT_SUB_LINK_PATTERN, MAX_PROXIES, PARSE_POOL, SubscriptionError, contains_sensitive_link, extract_sources,
    fetch_subscription, parse_uri, to_api_nodes,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("speed_bot")

FINAL_STATUSES = {"completed", "failed", "canceled"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

SELECTION_TTL = 600  # 选择菜单的有效期（秒）
DM_TARGET_TTL = 1800  # 私聊绑定的目标群有效期（秒）
BACKENDS_PER_PAGE = 5
SCRIPTS_PER_PAGE = 8
NOTICE_TTL = 120  # 群内引导消息自动删除时间（秒）
SUB_FETCH_TIMEOUT = 60  # 拉取一次提交中所有订阅的总超时（秒），防止慢速服务器拖住 bot
MAX_SUB_URLS = 5  # 一次最多拉取的订阅链接数
MAX_FILTER_LEN = 64
MEMBER_LABEL = "群友订阅"  # 群成员私聊提交的订阅在群里显示的名称
# 结果图排序方式（按钮文字, export 的 sort 参数；空字符串为订阅原顺序）
SORTS = [
    ("📋 订阅顺序（默认）", ""),
    ("🀄 节点名（升序）", "name_asc"),
    ("🚀 平均速度（升序）", "avg_speed_asc"),
    ("🚀 平均速度（降序）", "avg_speed_desc"),
    ("⏱ RTT（升序）", "rtt_asc"),
]


@dataclass
class DMTarget:
    """私聊提交的测速结果要发往的群。"""
    chat_id: int
    title: str
    updated: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.updated > DM_TARGET_TTL


@dataclass
class Selection:
    """测试提交前的选择状态（订阅 → 测试内容 → 后端 → 排序）。"""
    owner: User | None  # None 表示每日自动测速
    chat_id: int  # 进度和结果发往的群
    status: Message  # 菜单所在的消息（群里或私聊）
    chat_title: str = ""
    name_filter: str | None = None
    slave_arg: str | None = None  # -s 指定的后端，指定后跳过后端选择
    sub: tuple[str, str] | None = None  # (显示名称, 订阅链接或包含链接的文本)
    nodes: list[dict] = field(default_factory=list)
    skipped: int = 0
    slave: str | None = None  # 选定的后端 ID，None 表示自动选择
    slave_name: str | None = None
    backends: list[dict] = field(default_factory=list)  # 可选后端快照
    backend_page: int = 0
    sort: str | None = None  # None 表示未选择（按测试项目决定默认排序）
    options: set[str] = field(default_factory=lambda: set(PRESETS["speed"].options))
    tests_confirmed: bool = False
    scripts: list[dict] | None = None  # 全局脚本快照，仅含 id/name/type
    script_ids: set[str] = field(default_factory=set)
    script_page: int = 0
    script_error: str = ""
    warnings: str = ""  # 部分订阅失败或被跳过时的提示
    airport: bool = False  # 管理员测速本机场订阅（只有管理员可以提交）
    page: str = "subs"
    created: float = field(default_factory=time.monotonic)

    @property
    def label(self) -> str:
        return self.sub[0] if self.sub else "-"

    @property
    def test_summary(self) -> str:
        labels = [label for key, (label, _) in TEST_OPTIONS.items() if key in self.options]
        labels += [s.get("name") or s["id"] for s in self.scripts or [] if s["id"] in self.script_ids]
        return "、".join(labels) or "未选择"

    @property
    def plan(self) -> TestPlan:
        title = "测速" if self.options == set(PRESETS["speed"].options) and not self.script_ids else self.test_summary
        ids = tuple(s["id"] for s in self.scripts or [] if s["id"] in self.script_ids)
        return build_plan(title, self.options, ids)

    @property
    def sort_choices(self) -> list[int]:
        return [0, 1] + ([2, 3] if "speed" in self.options else [4] if "rtt" in self.options else [])


@dataclass
class TaskView:
    """已提交任务的展示信息。"""
    task_id: str
    label: str  # 订阅名
    info: str  # 发起人、节点数、任务 ID 等
    backend: str  # 选择的后端（自动选择时由任务状态中的 slave_name 覆盖）
    sort: str | None
    auto: bool = False  # 本机场自动测速（异常时提醒管理员）
    views: tuple[str, ...] = ("normalview",)


@dataclass
class ActiveTask:
    """进行中的测速任务（/tasks、/stopall、/cancel 使用）。"""
    task_id: str
    label: str
    requester: str  # 发起人（HTML）
    owner_id: int | None  # None 表示自动测速
    chat_ids: list[int]
    started: float = field(default_factory=time.monotonic)
    status: str = "pending"
    done: int = 0
    total: int = 0


@dataclass
class ResultPost:
    """_send_result 的结果。"""
    targets: list[Message]  # 确实收到了结果的目标消息
    messages: list[Message]  # 发出的结果消息（置顶用）
    entries: list[dict]  # 各节点结果（检查异常用）


HELP_TEXT = """<b>机场节点测速 Bot</b>

<b>测自己的订阅</b>：在群里发送 <code>/speed</code>，点击「🔒 私聊发送订阅」，在私聊里发送订阅链接，
选择测试内容（可多选测速、延迟、拓扑、流媒体等）、后端和排序后开始，进度和结果图会发回群里并 @ 你。{quota}
为防止泄露，群里出现的订阅链接会被自动删除。

<b>本机场节点状态</b>：{schedule}管理员可发送 <code>/speed</code> 或 <code>/autotest</code> 手动测速。

<b>命令</b>：
/speed — 测速
/sub — 本机场订阅、自动测速时间和你的剩余次数
/backends — 测试后端列表
/cancel — 取消自己的测速任务（回复进度消息，或带任务 ID）
/result 任务ID — 重新获取结果图

<b>可选参数</b>（跟在链接后面）：
<code>-f 关键词</code> 只测名称包含关键词的节点，多个用 | 分隔，例如 <code>-f "香港|HK"</code>
<code>-s 后端ID或名称</code> 直接指定测试后端"""


class SpeedBot(AdminCommands):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # 管理员在私聊里改过的设置覆盖 .env（先套用，后面的对象才能用上新值）
        self.settings = SettingsStore(os.path.join(cfg.data_dir, "settings.json"))
        self.settings.apply_to(cfg)
        self.api = SCPClient(cfg.api_key, cfg.api_base)
        self.quota = DailyQuota(os.path.join(cfg.data_dir, "usage.json"), cfg.daily_limit, cfg.timezone)
        self.stats = DailyStats(os.path.join(cfg.data_dir, "stats.json"), cfg.timezone)
        self.tz = ZoneInfo(cfg.timezone)
        self.schedule: Schedule | None = self._load_schedule(cfg.schedule_spec)
        self.schedule_changed = asyncio.Event()  # 时间表、订阅或群变化时唤醒定时任务重新计时
        self.started = time.monotonic()
        self.active: dict[str, ActiveTask] = {}  # 进行中的任务
        self.last_test: dict[int, float] = {}  # 群成员上次提交测速的时间（冷却）
        # /stopall 的次数：开始时记下它，之后变了就说明期间有人终止了所有任务（本轮自动测速、正在提交的任务）
        self._stop_gen = 0
        self._menu_ready: set[int] = set()  # 已设置好管理命令菜单的管理员
        self.running: dict[int, set[str]] = {}  # chat_id -> task_ids
        self.owners: dict[str, int] = {}  # task_id -> user_id
        self.selections: dict[str, Selection] = {}  # 菜单 id -> 选择状态
        self.dm_targets: dict[int, DMTarget] = {}  # user_id -> 私聊提交的结果去向
        self._backends: tuple[float, list[dict]] | None = None
        self._auto_lock = asyncio.Lock()
        self._loading: set[int] = set()  # 正在解析订阅的群成员，每人同时只能有一个
        self._parsing: dict[int, int] = {}  # 群成员仍在后台线程里跑的解析数（超时后线程不会停，跑完才算结束）
        self._timers: dict[asyncio.Task, Message] = {}  # 待执行的定时删除 -> 要删除的消息
        self._edit_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()

    # ------------------------------------------------------------ 群消息自动删除

    def expire(self, msg: Message | None, delay: float | None = None) -> None:
        """群里除测速结果外的消息（提示、菜单、进度、用户的命令）在 AUTO_DELETE_SECONDS 秒后删除。
        私聊消息不删（Telegram 中群聊 ID 为负数、私聊为正数）。"""
        delay = self.cfg.auto_delete_seconds if delay is None else delay
        if msg is None or delay <= 0 or msg.chat_id > 0:
            return
        task = asyncio.get_running_loop().create_task(self._delete_later(msg, delay))
        self._timers[task] = msg
        task.add_done_callback(lambda t: self._timers.pop(t, None))

    async def flush_expiring(self) -> None:
        """退出前把还没到时间的消息立即删掉，否则重启后它们会一直留在群里。"""
        pending = list(self._timers.items())
        self._timers.clear()
        for task, _ in pending:
            task.cancel()
        await asyncio.gather(*(t for t, _ in pending), return_exceptions=True)
        await asyncio.gather(*(self._delete_later(m, 0) for _, m in pending), return_exceptions=True)

    async def _say(self, msg: Message, text: str, **kw) -> Message:
        """回复一条临时消息（在群里会按 AUTO_DELETE_SECONDS 自动删除）。"""
        # 用户的命令可能已被定时删除（例如 API 较慢超过了删除时间），此时照常发出，只是不再引用
        kw.setdefault("allow_sending_without_reply", True)
        sent = await msg.reply_text(text, **kw)
        self.expire(sent)
        return sent

    # ------------------------------------------------------------ 权限与次数

    def _load_schedule(self, spec: str) -> Schedule | None:
        try:
            return parse_schedule(spec, self.tz)
        except ScheduleError as e:  # settings.json 被手动改坏
            log.warning("自动测速时间表「%s」无效，已关闭自动测速：%s", spec, e)
            return None

    def is_super(self, user_id: int | None) -> bool:
        """.env 中 ADMIN_USER_IDS 设置的超级管理员。"""
        return user_id is not None and user_id in self.cfg.admin_user_ids

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and (user_id in self.cfg.admin_user_ids or user_id in self.cfg.extra_admin_ids)

    def admin_ids(self) -> set[int]:
        return self.cfg.admin_user_ids | self.cfg.extra_admin_ids

    def is_banned(self, user_id: int | None) -> bool:
        return user_id in self.cfg.banned_user_ids and not self.is_admin(user_id)

    def _cooldown_left(self, user_id: int) -> int:
        """群成员还要等多少秒才能再次测速（管理员不受限）。"""
        last = self.last_test.get(user_id)
        if self.cfg.cooldown_seconds <= 0 or last is None or self.is_admin(user_id):
            return 0
        return max(0, math.ceil(last + self.cfg.cooldown_seconds - time.monotonic()))

    def _cooldown_text(self, user_id: int) -> str:
        return f"测速太频繁了，请 {fmt_duration(self._cooldown_left(user_id))}后再试。"

    def group_allowed(self, chat_id: int) -> bool:
        return not self.cfg.allowed_chat_ids or chat_id in self.cfg.allowed_chat_ids

    async def guard(self, update: Update) -> bool:
        """群命令只在授权群组中可用。"""
        chat = update.effective_chat
        self.expire(update.effective_message)  # 用户在群里发的命令也一并清理
        if chat.type == ChatType.PRIVATE:
            await self._say(update.effective_message, "请在机场群组里使用这个命令。")
            return False
        if not self.group_allowed(chat.id):
            log.info("拒绝来自 chat=%s 的请求", chat.id)
            await self._say(update.effective_message, f"本群未授权使用此 Bot。群组 ID：<code>{chat.id}</code>",
                                                      parse_mode=ParseMode.HTML)
            return False
        return True

    def _remaining(self, user_id: int) -> int | None:
        """今日剩余次数；管理员或未设上限时返回 None。"""
        return None if self.is_admin(user_id) else self.quota.remaining(user_id)

    def _quota_text(self, user_id: int) -> str:
        remaining = self._remaining(user_id)
        if remaining is None:
            return "你的测速次数不受限制。"
        return f"你今天还可以测速 {remaining}/{self.cfg.daily_limit} 次。"

    def _out_of_quota(self) -> str:
        return f"你今天的 {self.cfg.daily_limit} 次测速已用完，明天再来吧。"

    def _schedule_text(self) -> str:
        if not self.schedule or not self.cfg.subscriptions:
            return ""
        return f"{self.schedule.describe()} 自动测速并发到群里，"

    def next_run(self, now: datetime | None = None) -> datetime | None:
        """下一次自动测速的时间；未设置时间表时返回 None。"""
        if not self.schedule:
            return None
        now = now or datetime.now(self.tz)
        return self.schedule.next_after(now, self.tz)

    # ------------------------------------------------------------ 基础命令

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.expire(update.effective_message)
        quota = f"每人每天 {self.cfg.daily_limit} 次。" if self.cfg.daily_limit > 0 else ""
        text = HELP_TEXT.format(quota=quota, schedule=self._schedule_text())
        if update.effective_chat.type == ChatType.PRIVATE and self.is_admin(update.effective_user.id):
            text += "\n\n" + self.admin_help()
            await self.ensure_admin_menu(context.bot, update.effective_user.id)
        await self._say(update.effective_message, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.expire(update.effective_message)
        await self._say(update.effective_message, 
            f"群组 ID：<code>{update.effective_chat.id}</code>\n用户 ID：<code>{update.effective_user.id}</code>",
            parse_mode=ParseMode.HTML)

    async def cmd_sub(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        lines = []
        if self.cfg.subscriptions:
            names = "、".join(f"<code>{esc(name)}</code>" for name, _ in self.cfg.subscriptions)
            lines.append(f"<b>本机场订阅</b>：{names}")
            lines.append(self._schedule_text().rstrip("，") or "未设置自动测速。")
        lines.append(f"测自己的订阅：发送 /speed 后私聊发送链接。{self._quota_text(update.effective_user.id)}")
        await self._say(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_backends(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        try:
            backends = await self.api.list_backends()
        except APIError as e:
            await self._say(update.effective_message, f"获取后端失败：{esc(e)}")
            return
        if not backends:
            await self._say(update.effective_message, "暂无可用后端。")
            return
        lines = ["<b>后端列表</b>（🟢 在线 · 🔴 离线 · 🚫 不可选）"]
        for b in backends:
            state = "🟢" if b.get("is_online") else "🔴"
            if not self._backend_allowed(b):
                state = "🚫"
            lines.append(
                f"{state} <b>{esc(b.get('display_name') or '-')}</b>\n"
                f"    ID <code>{esc(b.get('client_id'))}</code> · 排队 {b.get('speed_pending', 0)}"
            )
        await self._say(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        msg = update.effective_message
        task_id = self._task_id_from(msg, context.args)
        if not task_id:
            await self._say(msg, "用法：/result 任务ID，或回复任务消息。")
            return
        await self._send_result(msg, task_id, "avg_speed_desc", f"任务 <code>{task_id}</code>")

    @staticmethod
    def _task_id_from(msg: Message, args: list[str] | None) -> str | None:
        """命令参数或被回复的消息（进度消息、结果图）里的任务 ID。"""
        texts = [" ".join(args or [])]
        if msg.reply_to_message:
            texts.append(msg.reply_to_message.text or msg.reply_to_message.caption or "")
        return next((m.group(0) for m in map(UUID_RE.search, texts) if m), None)

    async def _cancel(self, task_id: str, user_id: int) -> str:
        owner = self.owners.get(task_id)
        if owner != user_id and not self.is_admin(user_id):
            return "只有任务发起人或管理员可以取消该任务。"
        try:
            await self.api.cancel_task(task_id)
        except APIError as e:
            return f"取消失败：{e}"
        return "已发起取消请求。"

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        data = q.data or ""
        if data.startswith("group:"):
            await self._on_group_select(q, context)
            return
        if data.startswith("sel:"):
            await self._on_select(q, context)
            return
        action, _, task_id = data.partition(":")
        if action != "cancel":
            await q.answer()
            return
        await q.answer(await self._cancel(task_id, q.from_user.id), show_alert=False)

    # ------------------------------------------------------------ 群内订阅保护与私聊引导

    def _dm_markup(self, bot_username: str, chat_id: int) -> InlineKeyboardMarkup:
        url = f"https://t.me/{bot_username}?start=g{chat_id}"
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔒 私聊发送订阅", url=url)]])

    async def _group_choices(self, bot) -> list[tuple[int, str, str | None]]:
        """返回授权群的 (ID, 名称, 加群链接)；链接来自公开用户名或 Telegram 已知邀请链接。"""
        choices = []
        for chat_id in sorted(self.cfg.allowed_chat_ids):
            try:
                chat = await bot.get_chat(chat_id)
                title = getattr(chat, "title", None) or str(chat_id)
                username = (getattr(chat, "username", None) or "").lstrip("@")
                link = f"https://t.me/{username}" if username else getattr(chat, "invite_link", None)
            except TelegramError:
                title, link = str(chat_id), None
            choices.append((chat_id, title, link))
        return choices

    async def _send_group_picker(self, msg: Message, bot, intro: str = "") -> None:
        """私聊没有绑定目标群时，列出授权群，避免用户不知道应加入哪个群。"""
        if not self.cfg.allowed_chat_ids:
            await self._say(msg, intro + "请先在 Bot 所在的测速群里发送 /speed，再点击私聊按钮。")
            return
        choices = await self._group_choices(bot)
        rows = []
        for chat_id, title, link in choices:
            label = f"选择「{title[:30]}」"
            row = [InlineKeyboardButton(label, callback_data=f"group:{chat_id}")]
            if link:
                row.insert(0, InlineKeyboardButton(f"加入「{title[:24]}」", url=link))
            rows.append(row)
        text = (intro + "请先加入一个测速群，再点击‘选择此群’。\n\n"
                "授权群列表：\n" + "\n".join(
                    f"• <b>{esc(title)}</b> <code>{chat_id}</code>" for chat_id, title, _ in choices))
        await self._say(msg, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

    async def _on_group_select(self, q, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            chat_id = int((q.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            await q.answer("群组选择无效。", show_alert=True)
            return
        if not self.group_allowed(chat_id):
            await q.answer("该群已不在授权名单中。", show_alert=True)
            return
        if self.is_banned(q.from_user.id):
            await q.answer("你已被管理员禁止使用测速。", show_alert=True)
            return
        if not self.is_admin(q.from_user.id):
            member = await self._is_member(context.bot, chat_id, q.from_user.id)
            if member is None:
                await q.answer("暂时无法确认群成员身份，请稍后重试。", show_alert=True)
                return
            if not member:
                await q.answer("请先点击‘加入’进入该群，再选择此群。", show_alert=True)
                return
        title = await self._chat_title(context.bot, chat_id)
        self.dm_targets[q.from_user.id] = DMTarget(chat_id, title)
        await q.answer("已选择此群。")
        message = getattr(q, "message", None)
        if message:
            await message.edit_text(
                f"好的，测速结果将发送到群「{esc(title)}」。{self._quota_text(q.from_user.id)}\n\n"
                "请直接发送订阅链接或节点链接。", parse_mode=ParseMode.HTML)

    async def _send_dm_prompt(self, msg: Message, user: User, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                              deleted: bool = False) -> None:
        text = (f"{user.mention_html()} " + ("已删除你发送的订阅/节点链接，避免泄露。\n" if deleted else "")
                + "请点击下方按钮，在私聊中发送订阅链接测速，结果会发回本群。")
        markup = self._dm_markup(context.bot.username, chat_id)
        try:
            if deleted:
                notice = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                notice = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError as e:
            log.warning("发送私聊引导失败：%s", e)
            return
        # 和其他提示一样按 AUTO_DELETE_SECONDS 删除；关闭自动删除时仍在 NOTICE_TTL 后清理这条引导
        self.expire(notice, self.cfg.auto_delete_seconds or NOTICE_TTL)

    async def _delete(self, msg: Message | None) -> bool:
        if msg is None:
            return False
        try:
            await msg.delete()
            return True
        except TelegramError as e:
            log.warning("删除含订阅链接的消息失败（bot 需要是群管理员并有删除消息权限）：%s", e)
            return False

    async def _delete_later(self, msg: Message, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await msg.delete()
        except TelegramError as e:  # 已被删除、超过 48 小时或没有删除权限
            log.debug("定时删除消息失败：%s", e)

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """授权群内出现节点链接或疑似订阅链接时立即删除，并引导发送者私聊测速。"""
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not msg or not self.cfg.delete_sub_message or not user or user.is_bot:
            return
        if not self.group_allowed(chat.id):
            return
        pattern = self.cfg.sub_link_pattern or DEFAULT_SUB_LINK_PATTERN
        if not contains_sensitive_link(msg.text or msg.caption or "", pattern):
            return
        log.info("删除群 %s 中用户 %s 发送的订阅链接", chat.id, user.id)
        deleted = await self._delete(msg)
        await self._send_dm_prompt(msg, user, chat.id, context, deleted=deleted)
        raise ApplicationHandlerStop

    async def _is_member(self, bot, chat_id: int, user_id: int) -> bool | None:
        """True/False：确定是/不是群成员；None：暂时查询失败（超时、网络、限流），不能据此判定已退群。"""
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except (BadRequest, Forbidden):  # 用户或群无效、bot 已不在群里：确定不能测速
            return False
        except TelegramError as e:
            log.warning("查询群 %s 成员 %s 失败：%s", chat_id, user_id, e)
            return None
        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)

    async def _check_member(self, user: User, chat_id: int, chat_title: str, bot, notify) -> bool:
        """群成员每次发链接和每次提交都重新确认身份，被踢出或退群的人不能继续往群里发测速。"""
        if self.is_admin(user.id):
            return True
        member = await self._is_member(bot, chat_id, user.id)
        if member:
            return True
        if member is None:
            await notify("暂时无法确认你的群成员身份，请稍后再试。")
        else:
            self.dm_targets.pop(user.id, None)
            await notify(f"你已不是群「{esc(chat_title)}」的成员，无法为该群测速。", parse_mode=ParseMode.HTML)
        return False

    async def _chat_title(self, bot, chat_id: int) -> str:
        try:
            return (await bot.get_chat(chat_id)).title or str(chat_id)
        except TelegramError:
            return str(chat_id)

    # ------------------------------------------------------------ /speed

    @staticmethod
    def _parse_args(args: list[str]) -> tuple[list[str], str | None, str | None]:
        rest, name_filter, slave = [], None, None
        it = iter(args)
        for a in it:
            if a in ("-f", "--filter"):
                name_filter = next(it, None)
            elif a in ("-s", "--slave"):
                slave = next(it, None)
            else:
                rest.append(a)
        if name_filter:
            name_filter = name_filter.strip("\"'“”")
        return rest, name_filter, slave

    def _busy(self, chat_id: int) -> bool:
        return len(self.running.get(chat_id, ())) >= self.cfg.max_tasks_per_chat

    def _match_sub(self, name: str) -> tuple[str, str] | None:
        subs = self.cfg.subscriptions
        return (next((s for s in subs if s[0] == name), None)
                or next((s for s in subs if s[0].lower() == name.lower()), None))

    async def cmd_speed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if chat.type == ChatType.PRIVATE:
            await self._private_speed(update, context, context.args or [])
            return
        if not await self.guard(update):
            return
        has_links = any(extract_sources(msg.text or ""))
        if self.is_banned(user.id):
            deleted = await self._delete(msg) if has_links and self.cfg.delete_sub_message else False
            await self._say(msg, "你已被管理员禁止使用测速。" + ("（含订阅链接的消息已删除）" if deleted else ""))
            return
        if not self.is_admin(user.id) or has_links or not self.cfg.subscriptions:
            # 群成员（以及带链接的命令）：引导私聊发送订阅，避免链接出现在群里
            deleted = await self._delete(msg) if has_links and self.cfg.delete_sub_message else False
            await self._send_dm_prompt(msg, user, chat.id, context, deleted=deleted)
            return

        # 管理员：测速本机场的固定订阅
        rest, name_filter, slave = self._parse_args(context.args or [])
        if name_filter and not self._valid_filter(name_filter):
            await self._say(msg, f"过滤关键词太长（最多 {MAX_FILTER_LEN} 个字符）。")
            return
        if self._busy(chat.id):
            await self._say(msg, "本群已有测速任务在运行，请等待完成后再试。")
            return
        sub = None
        if rest:
            sub = self._match_sub(rest[0])
            if not sub:
                names = "、".join(f"<code>{esc(n)}</code>" for n, _ in self.cfg.subscriptions)
                await self._say(msg, f"未找到订阅「{esc(rest[0])}」。本机场订阅：{names}", parse_mode=ParseMode.HTML)
                return
        elif len(self.cfg.subscriptions) == 1:
            sub = self.cfg.subscriptions[0]

        self._purge_selections()
        sid = secrets.token_hex(4)
        if sub is None:
            status = await msg.reply_text("📋 正在加载订阅…")
            sel = Selection(owner=user, chat_id=chat.id, status=status, name_filter=name_filter, slave_arg=slave,
                            airport=True)
            self.selections[sid] = sel
            self._schedule_purge(sel)
            await self._render_menu(sid, sel)
            return
        status = await msg.reply_text(f"📥 任务 <b>{esc(sub[0])}</b> 正在解析节点…", parse_mode=ParseMode.HTML)
        sel = Selection(owner=user, chat_id=chat.id, status=status, name_filter=name_filter, slave_arg=slave, sub=sub,
                        airport=True)
        if await self._prepare(sel):
            await self._next_step(sid, sel, context.application)

    @staticmethod
    def _valid_filter(keywords: str) -> bool:
        """-f 是 | 分隔的关键词（不是正则），只限制长度。"""
        return len(keywords) <= MAX_FILTER_LEN

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """处理群里「私聊发送订阅」按钮的深链：/start g<群ID>。"""
        msg, user = update.effective_message, update.effective_user
        m = re.fullmatch(r"g(-?\d+)(?:_\w+)?", (context.args or [""])[0])
        if update.effective_chat.type != ChatType.PRIVATE:
            await self.cmd_help(update, context)  # 管理员的命令菜单在这里补设
            return
        await self.ensure_admin_menu(context.bot, user.id)
        if not m:
            await self._send_group_picker(msg, context.bot)
            return
        chat_id = int(m.group(1))
        if not self.group_allowed(chat_id):
            await self._say(msg, "该群未授权使用此 Bot。")
            return
        if self.is_banned(user.id):
            await self._say(msg, "你已被管理员禁止使用测速。")
            return
        if not self.is_admin(user.id):
            member = await self._is_member(context.bot, chat_id, user.id)
            if member is None:
                await self._say(msg, "暂时无法确认你的群成员身份，请稍后再点一次按钮。")
                return
            if not member:
                await self._say(msg, "你不是该群成员，无法为该群测速。")
                return
        title = await self._chat_title(context.bot, chat_id)
        self.dm_targets[user.id] = DMTarget(chat_id, title)
        await self._say(msg, 
            f"好的，测速结果将发送到群「{esc(title)}」。{self._quota_text(user.id)}\n\n"
            f"请直接发送订阅链接或节点链接（可附加 <code>-f 关键词</code> 过滤节点、<code>-s 后端ID</code> 指定后端）。",
            parse_mode=ParseMode.HTML)

    async def on_private_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not any(extract_sources(msg.text or "")):
            if not await self._resolve_target(update.effective_user, context.bot):
                await self._send_group_picker(msg, context.bot)
            else:
                await self._say(msg, "请发送订阅链接或节点链接。发送 /help 查看用法。")
            return
        await self._private_speed(update, context, (msg.text or "").split())

    async def _resolve_target(self, user: User, bot) -> DMTarget | None:
        """私聊提交的结果发到哪个群：最近点过按钮的群 > 唯一授权群（需是群成员）。"""
        target = self.dm_targets.get(user.id)
        if target and not target.expired() and self.group_allowed(target.chat_id):  # 群可能已被移出白名单
            return target
        if len(self.cfg.allowed_chat_ids) == 1:
            chat_id = next(iter(self.cfg.allowed_chat_ids))
            if self.is_admin(user.id) or await self._is_member(bot, chat_id, user.id):
                target = DMTarget(chat_id, await self._chat_title(bot, chat_id))
                self.dm_targets[user.id] = target
                return target
        return None

    async def _private_speed(self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
        """群成员在私聊里提交任意订阅测速。"""
        msg, user = update.effective_message, update.effective_user
        if self.is_banned(user.id):
            await self._say(msg, "你已被管理员禁止使用测速。")
            return
        target = await self._resolve_target(user, context.bot)
        if not target:
            await self._send_group_picker(msg, context.bot,
                                          "还没有选择接收测速结果的群。\n")
            return
        target.updated = time.monotonic()
        rest, name_filter, slave = self._parse_args(args)
        subs, uris = extract_sources(" ".join(rest))
        if not subs and not uris and msg.reply_to_message:
            reply = msg.reply_to_message
            subs, uris = extract_sources(reply.text or reply.caption or "")
        if not subs and not uris:
            await self._say(msg, "请发送订阅链接或节点链接，例如：\n<code>https://example.com/sub -f 香港</code>",
                                 parse_mode=ParseMode.HTML)
            return
        if name_filter and not self._valid_filter(name_filter):
            await self._say(msg, f"过滤关键词太长（最多 {MAX_FILTER_LEN} 个字符）。")
            return
        if not await self._check_member(user, target.chat_id, target.title, context.bot, msg.reply_text):
            return
        if self._remaining(user.id) == 0:
            await self._say(msg, self._out_of_quota())
            return
        if self._cooldown_left(user.id):
            await self._say(msg, self._cooldown_text(user.id))
            return
        if self._busy(target.chat_id):
            await self._say(msg, "群里已有测速任务在运行，请等待完成后再试。")
            return

        # 每人同时只能解析一个订阅（检查和登记之间没有 await，并发消息也只有一个能通过）
        if user.id in self._loading or self._parsing.get(user.id):
            await self._say(msg, "你的上一个订阅还在解析中，请等它完成后再发送。")
            return
        self._loading.add(user.id)
        try:
            status = await msg.reply_text("📥 正在解析节点…")
            sel = Selection(owner=user, chat_id=target.chat_id, chat_title=target.title, status=status,
                            name_filter=name_filter, slave_arg=slave, sub=(MEMBER_LABEL, " ".join(subs + uris)))
            self._purge_selections()
            if await self._prepare(sel):
                await self._next_step(secrets.token_hex(4), sel, context.application)
        finally:
            self._loading.discard(user.id)

    def _track_parse(self, user_id: int, future) -> None:
        """记录群成员的后台解析，线程真正结束后才释放（等待被超时取消时线程仍在跑）。"""
        loop = asyncio.get_running_loop()
        self._parsing[user_id] = self._parsing.get(user_id, 0) + 1

        def done(_):
            def release():
                left = self._parsing.get(user_id, 1) - 1
                if left > 0:
                    self._parsing[user_id] = left
                else:
                    self._parsing.pop(user_id, None)
            loop.call_soon_threadsafe(release)

        future.add_done_callback(done)

    async def _load_nodes(self, sel: Selection) -> str | None:
        """拉取订阅、解析节点，并确定可选后端。成功返回 None，失败返回要显示的错误信息。"""
        subs, uris = extract_sources(sel.sub[1])
        proxies: list[dict] = []
        errors: list[str] = []
        subs = list(dict.fromkeys(subs))
        if len(subs) > MAX_SUB_URLS:
            errors.append(f"一次最多测试 {MAX_SUB_URLS} 个订阅，其余已忽略")
            subs = subs[:MAX_SUB_URLS]
        for uri in list(dict.fromkeys(uris))[:MAX_PROXIES]:
            p = parse_uri(uri)
            if p:
                proxies.append(p)
            else:
                errors.append("有节点链接无法解析")
        # 本机场订阅（自动测速、管理员）用默认线程池，群成员的订阅用专用小线程池，互不影响
        trusted = sel.owner is None or self.is_admin(sel.owner.id)
        executor = None if trusted else PARSE_POOL
        on_parse = None if trusted else (lambda f: self._track_parse(sel.owner.id, f))

        async def fetch_all() -> None:
            for url in subs:
                if len(proxies) >= MAX_PROXIES:
                    break
                try:
                    got = await fetch_subscription(url, executor=executor, on_parse=on_parse)
                    if not got:
                        errors.append("订阅中没有找到节点")
                    proxies.extend(got[:MAX_PROXIES - len(proxies)])
                except SubscriptionError as e:
                    errors.append(str(e))
                except Exception:  # 无效链接（如 https://[abc/sub）等意外错误：跳过这个链接，其余照常
                    log.warning("拉取订阅出错", exc_info=True)  # 不记录链接本身，里面可能有 token
                    errors.append("获取订阅失败：链接无效或内容无法解析")

        try:
            await asyncio.wait_for(fetch_all(), SUB_FETCH_TIMEOUT)  # 所有订阅共用一个总超时
        except asyncio.TimeoutError:  # Python 3.10 中与内置 TimeoutError 不是同一个类
            errors.append("获取订阅超时，未完成的订阅已跳过")
        limit = self.cfg.max_nodes
        capped = not trusted and 0 < self.cfg.member_max_nodes < limit
        if capped:
            limit = self.cfg.member_max_nodes
        try:
            # 节点很多时转换也比较耗时，放到线程里
            sel.nodes, sel.skipped = await asyncio.to_thread(to_api_nodes, proxies, sel.name_filter, limit)
        except Exception:
            log.warning("转换节点出错", exc_info=True)
            sel.nodes, sel.skipped = [], len(proxies)
            errors.append("节点内容无法解析")
        if capped and len(sel.nodes) >= limit and sel.skipped:
            errors.append(f"每次最多测试 {limit} 个节点，其余已跳过（可用 -f 关键词挑选节点）")
        if not sel.nodes:
            detail = "；".join(dict.fromkeys(errors)) or "没有符合条件的节点"
            return f"❌ 任务 <b>{esc(sel.label)}</b> 没有可测试的节点：{esc(detail)}"
        # 部分成功时也告诉用户哪些订阅被跳过了
        sel.warnings = "；".join(dict.fromkeys(errors))

        sel.backends = await self._selectable_backends()
        chosen = None
        if sel.slave_arg:
            chosen = self._match_backend(sel.backends, sel.slave_arg)
            if not chosen:
                return (f"❌ 后端 <code>{esc(sel.slave_arg)}</code> 不存在、离线或不允许使用，"
                        f"发送 /backends 查看可用后端。")
        elif self.cfg.default_slave_id:
            chosen = self._match_backend(sel.backends, self.cfg.default_slave_id)
        if chosen:
            sel.slave, sel.slave_name = chosen["client_id"], chosen.get("display_name")
        return None

    async def _prepare(self, sel: Selection) -> bool:
        """加载节点；失败时在状态消息里说明。"""
        error = await self._load_nodes(sel)
        if error:
            await self._edit(sel.status, error)
            self.expire(sel.status)
        return error is None

    def _advance(self, sid: str, sel: Selection) -> bool:
        """决定下一步：需要选内容/后端/排序时切换页面并返回 False；可以提交时移除菜单并返回 True。
        这里不 await，保证同一菜单被并发点击时只会提交一次。"""
        if sel.owner is not None and not sel.tests_confirmed:
            sel.page = "tests"
        elif self.cfg.backend_select and not sel.slave_arg and len(sel.backends) > 1 and sel.page != "sort":
            sel.page = "backends"
        elif self.cfg.sort_select and sel.sort is None and "normalview" in sel.plan.views:
            sel.page = "sort"
        else:
            self.selections.pop(sid, None)
            sel.page = "submitting"
            return True
        if sid not in self.selections:
            self._schedule_purge(sel)
        self.selections[sid] = sel
        return False

    async def _next_step(self, sid: str, sel: Selection, application: Application) -> None:
        """节点就绪后：选测试内容 → 按需选后端 → 选排序 → 提交。"""
        if self._advance(sid, sel):
            await self._submit(sel, application)
        else:
            await self._render_menu(sid, sel)

    # ------------------------------------------------------------ 自动测速

    def seconds_until_next_run(self, now: datetime | None = None) -> float | None:
        """距离下一次自动测速的秒数；未设置时间表时返回 None。"""
        now = now or datetime.now(self.tz)
        nxt = self.next_run(now)
        if nxt is None:
            return None
        # 同一 tzinfo 的 aware datetime 相减按墙上时间、忽略 UTC 偏移，换成 UTC，夏令时切换日也准确
        return (nxt.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()

    def _auto_chats(self) -> list[int]:
        """自动测速发往的群：AUTO_CHAT_IDS（留空为所有授权群），已被移出授权群的不再发送。"""
        return sorted(c for c in (self.cfg.auto_chat_ids or self.cfg.allowed_chat_ids) if self.group_allowed(c))

    async def scheduler(self, application: Application) -> None:
        """按时间表自动测速本机场订阅并发到群里。管理员修改时间表、订阅或授权群后立即按新设置重新计时。"""
        while True:
            self.schedule_changed.clear()
            delay = self.seconds_until_next_run()
            if delay is None or not self.cfg.subscriptions or not self._auto_chats():
                log.info("自动测速未启用（%s）", "未设置时间表" if delay is None else "没有本机场订阅或目标群")
                await self.schedule_changed.wait()
                continue
            log.info("下次自动测速在 %.0f 秒后", delay)
            try:
                await asyncio.wait_for(self.schedule_changed.wait(), delay)
                continue  # 设置变了，重新计算下一次的时间
            except asyncio.TimeoutError:
                pass
            try:
                # 管理员手动测速正在进行时，等它结束后再跑，不跳过这一次定时测速
                await self.run_auto(application, self._auto_chats(), "🕘 定时自动测速", wait_for_lock=True)
            except Exception:
                log.exception("自动测速出错")
            await asyncio.sleep(1)  # 避免同一分钟内重复触发

    async def run_auto(self, application: Application, chat_ids: list[int], title: str,
                       wait_for_lock: bool = False) -> bool:
        """依次测速所有本机场订阅（每个订阅只提交一次，结果同时发到所有目标群），每个等上一个完成后再开始。
        已有自动测速在进行且 wait_for_lock=False 时返回 False。"""
        if self._auto_lock.locked() and not wait_for_lock:
            return False
        gen = self._stop_gen  # 排队等锁期间的 /stopall 同样作数
        async with self._auto_lock:
            posted: dict[int, list[Message]] = {}
            for sub in list(self.cfg.subscriptions):
                if self._stop_gen != gen:
                    break
                statuses = []
                for chat_id in chat_ids:
                    try:
                        statuses.append(await application.bot.send_message(
                            chat_id, f"{title} · 任务 <b>{esc(sub[0])}</b> 正在解析节点…", parse_mode=ParseMode.HTML))
                    except TelegramError as e:
                        log.warning("向群 %s 发送自动测速消息失败：%s", chat_id, e)
                if not statuses:
                    continue
                sel = Selection(owner=None, chat_id=statuses[0].chat_id, status=statuses[0], sub=sub,
                                slave_arg=None, sort="avg_speed_desc")
                error = await self._load_nodes(sel)
                stopped = self._stop_gen != gen
                if not error and stopped:
                    error = f"🚫 任务 <b>{esc(sub[0])}</b> 已被管理员终止。"
                if error:
                    for m in statuses:
                        await self._edit(m, error)
                        self.expire(m)
                    if not stopped and self.cfg.anomaly_percent > 0:
                        await self._notify_admins(
                            application.bot, f"⚠️ 本机场订阅「{esc(sub[0])}」自动测速没能开始：\n{error}")
                    continue
                if self.cfg.auto_slave_id:
                    chosen = self._match_backend(sel.backends, self.cfg.auto_slave_id)
                    if chosen:
                        sel.slave, sel.slave_name = chosen["client_id"], chosen.get("display_name")
                for m in await self._submit(sel, application, requester=title, wait=True, mirrors=statuses[1:]):
                    posted.setdefault(m.chat_id, []).append(m)
            if self.cfg.pin_auto_result:
                await self._pin_results(application.bot, posted)
        return True

    async def _pin_results(self, bot, posted: dict[int, list[Message]]) -> None:
        """置顶这一轮的本机场测速结果，并取消置顶上一轮的（需要 bot 有置顶消息权限）。"""
        for chat_id, msgs in posted.items():
            for old in self.settings.pinned(chat_id):
                try:
                    await bot.unpin_chat_message(chat_id, message_id=old)
                except TelegramError as e:  # 已被手动取消置顶或删除
                    log.debug("取消置顶 %s 失败：%s", old, e)
            pinned = []
            for m in msgs:
                try:
                    await bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
                    pinned.append(m.message_id)
                except TelegramError as e:
                    log.warning("置顶群 %s 的测速结果失败（bot 需要有置顶消息权限）：%s", chat_id, e)
                    break
            try:
                self.settings.set_pinned(chat_id, pinned)
            except OSError as e:
                log.warning("保存置顶记录失败：%s", e)

    async def on_pinned(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """bot 置顶结果时 Telegram 会在群里发一条“置顶了消息”的通知，删掉它，群里只留测速结果。"""
        msg = update.effective_message
        if msg and msg.from_user and msg.from_user.id == context.bot.id:
            await self._delete_later(msg, 0)

    async def _notify_admins(self, bot, text: str) -> None:
        """私聊提醒所有管理员（异常、积分）。管理员需要先私聊过 bot 才能收到。"""
        for uid in sorted(self.admin_ids()):
            try:
                await bot.send_message(uid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except TelegramError as e:
                log.info("向管理员 %s 发送提醒失败（需要先私聊 bot 发送 /start）：%s", uid, e)

    async def cmd_autotest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """管理员手动触发一次本机场订阅测速（与定时自动测速相同）。群里发结果到本群，私聊里发到所有自动测速群。"""
        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        private = chat.type == ChatType.PRIVATE
        if not private and not await self.guard(update):
            return
        if not self.is_admin(user.id):
            await self._say(msg, "只有管理员可以手动触发本机场测速。")
            return
        if not self.cfg.subscriptions:
            await self._say(msg, "还没有配置本机场订阅，可以在私聊里用 /airport add 添加。")
            return
        chats = self._auto_chats() if private else [chat.id]
        if not chats:
            await self._say(msg, "还没有授权群，先用 /group add 群组ID 添加。")
            return
        if self._auto_lock.locked():
            await self._say(msg, "本机场测速正在进行中，请等待完成。")
            return
        names = "、".join(n for n, _ in self.cfg.subscriptions)
        where = f"，结果发到 {len(chats)} 个群" if private else ""
        await self._say(msg, f"开始测速本机场订阅：{esc(names)}{where}", parse_mode=ParseMode.HTML)
        context.application.create_task(
            self.run_auto(context.application, chats, f"🛠 管理员 {user.mention_html()} 手动测速"))

    # ------------------------------------------------------------ 选择菜单

    def _purge_selections(self) -> None:
        now = time.monotonic()
        # 正在加载的菜单还在使用中：等加载结束（提交 / 失败 / 回到菜单页）后再按过期处理
        for sid in [k for k, s in self.selections.items()
                    if now - s.created > SELECTION_TTL and s.page != "loading"]:
            sel = self.selections.pop(sid, None)
            if sel:
                self.expire(sel.status, 0.1)  # 过期没人点的菜单直接清理

    def _schedule_purge(self, sel: Selection) -> None:
        """菜单到期后自动清理，不依赖之后有人再发 /speed。"""
        delay = max(0.0, sel.created + SELECTION_TTL - time.monotonic()) + 1
        asyncio.get_running_loop().call_later(delay, self._purge_selections)

    async def _all_backends(self) -> list[dict]:
        """后端列表，缓存 30 秒。"""
        if self._backends is None or time.monotonic() - self._backends[0] > 30:
            try:
                backends = await self.api.list_backends()
            except APIError as e:
                log.warning("获取后端列表失败：%s", e)
                return self._backends[1] if self._backends else []
            self._backends = (time.monotonic(), backends)
        return self._backends[1]

    def _backend_allowed(self, b: dict) -> bool:
        if not b.get("client_id") or b.get("allow_public_access") is False or b.get("locked") or b.get("upgrade_required"):
            return False
        return not self.cfg.allowed_backends or b.get("client_id") in self.cfg.allowed_backends

    async def _selectable_backends(self, refresh: bool = False) -> list[dict]:
        """可选后端：在线、未锁定、无需升级且有调用权限，保持 API 返回的顺序。
        提交时刷新列表，查询失败直接报错，不用旧缓存分配任务。"""
        if refresh:
            backends = await self.api.list_backends()
            self._backends = (time.monotonic(), backends)
        else:
            backends = await self._all_backends()
        return [b for b in backends if b.get("is_online") and self._backend_allowed(b)]

    @staticmethod
    def _match_backend(backends: list[dict], key: str) -> dict | None:
        """按后端 ID 或显示名称（不区分大小写）查找。"""
        for b in backends:
            if b.get("client_id") == key:
                return b
        key = key.lower()
        return next((b for b in backends if (b.get("display_name") or "").lower() == key), None)

    @staticmethod
    def _backend_label(b: dict) -> str:
        name, cid = b.get("display_name") or b.get("client_id"), b.get("client_id")
        return name if name == cid else f"{name} ({cid})"

    async def _render_menu(self, sid: str, sel: Selection) -> None:
        # 按钮应答期间可能已提交、取消或过期，不能再用旧菜单覆盖进度消息。
        if self.selections.get(sid) is not sel or sel.page not in {"subs", "tests", "scripts", "backends", "sort"}:
            return

        def btn(label: str, action: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(label, callback_data=f"sel:{sid}:{action}")

        rows: list[list[InlineKeyboardButton]] = []
        who = sel.owner.mention_html() if sel.owner else ""
        if sel.page == "subs":
            text = f"📋 <b>选择要测速的订阅</b> · {who}"
            buttons = [btn(name[:30], f"u:{n}") for n, (name, _) in enumerate(self.cfg.subscriptions)]
            rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        elif sel.page == "tests":
            text = (f"🧪 <b>选择测试内容（可多选）</b> · {who}\n\n"
                    f"任务：<b>{esc(sel.label)}</b>\n节点 {len(sel.nodes)} 个\n"
                    f"已选：{esc(sel.test_summary[:800])}\n点击项目勾选或取消，确认后继续。")
            buttons = [btn(f"{'✅' if key in sel.options else '⬜'} {label}", f"t:{key}")
                       for key, (label, _) in TEST_OPTIONS.items()]
            rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            rows.append([btn(f"🎬 流媒体 / 脚本（已选 {len(sel.script_ids)} 项）", "scripts")])
            rows.append([btn("清空选择", "clear"), btn("✅ 确认测试内容", "go")])
        elif sel.page == "scripts":
            text = (f"🎬 <b>选择流媒体 / 检测脚本（可多选）</b> · {who}\n\n"
                    f"已选 {len(sel.script_ids)} 项，点击脚本勾选或取消。")
            scripts = sel.scripts or []
            if sel.script_error:
                text += f"\n⚠️ 获取流媒体脚本失败：{esc(sel.script_error[:300])}\n返回后可重试。"
            elif not scripts:
                text += "\n后端暂无可用流媒体脚本，可返回选择其他测试项目。"
            pages = max(1, -(-len(scripts) // SCRIPTS_PER_PAGE))
            sel.script_page = min(sel.script_page, pages - 1)
            start = sel.script_page * SCRIPTS_PER_PAGE
            for n, s in enumerate(scripts[start:start + SCRIPTS_PER_PAGE], start):
                mark = "✅" if s["id"] in sel.script_ids else "⬜"
                rows.append([btn(f"{mark} {(s.get('name') or s['id'])[:45]}", f"s:{n}")])
            if pages > 1:
                rows.append([
                    btn("上一页", f"sp:{sel.script_page - 1}") if sel.script_page > 0 else btn("·", "noop"),
                    btn(f"{sel.script_page + 1}/{pages}", "noop"),
                    btn("下一页", f"sp:{sel.script_page + 1}") if sel.script_page < pages - 1 else btn("·", "noop"),
                ])
            rows.append([btn("返回测试内容", "back")])
        elif sel.page == "backends":
            text = f"🖥 <b>选择测速后端</b> · {who}\n\n任务：<b>{esc(sel.label)}</b>\n节点 {len(sel.nodes)} 个"
            pages = max(1, -(-len(sel.backends) // BACKENDS_PER_PAGE))
            sel.backend_page = min(sel.backend_page, pages - 1)
            start = sel.backend_page * BACKENDS_PER_PAGE
            if sel.backend_page == 0:
                rows.append([btn("🤖 自动选择", "b:auto")])
            for n, b in enumerate(sel.backends[start:start + BACKENDS_PER_PAGE], start):
                rows.append([btn(self._backend_label(b)[:48], f"b:{n}")])
            if pages > 1:
                rows.append([
                    btn("上一页", f"bp:{sel.backend_page - 1}") if sel.backend_page > 0 else btn("·", "noop"),
                    btn(f"{sel.backend_page + 1}/{pages}", "noop"),
                    btn("下一页", f"bp:{sel.backend_page + 1}") if sel.backend_page < pages - 1 else btn("·", "noop"),
                ])
        else:  # sort
            text = (f"📊 <b>选择排序方式</b> · {who}\n\n"
                    f"订阅名：<b>{esc(sel.label)}</b>\n选中后端：<b>{esc(sel.slave or '自动选择')}</b>")
            text += f"\n测试内容：{esc(sel.test_summary[:800])}"
            buttons = [btn(SORTS[n][0], f"o:{n}") for n in sel.sort_choices]
            rows += [buttons[:1], buttons[1:2], buttons[2:]]
        if sel.warnings and sel.page != "subs":
            text += f"\n⚠️ {esc(sel.warnings)}"
        if sel.chat_id != sel.status.chat_id and sel.page != "subs":
            text += f"\n结果将发送到群「{esc(sel.chat_title)}」"
        rows.append([btn("❌ 终止操作", "x")])
        await self._edit(sel.status, text, InlineKeyboardMarkup(rows))

    async def _on_select(self, q, context: ContextTypes.DEFAULT_TYPE) -> None:
        _, sid, action = (q.data or "").split(":", 2)
        sel = self.selections.get(sid)
        if not sel or time.monotonic() - sel.created > SELECTION_TTL:
            self.selections.pop(sid, None)
            # 过期菜单被点击时一并清理；sel 为 None（可能正被 _submit 用作进度消息）或仍在加载中时不删
            if sel and sel.page != "loading":
                self.expire(sel.status, 0.1)
            await q.answer("选择已过期，请重新发送 /speed。", show_alert=True)
            return
        if q.from_user.id != sel.owner.id and not self.is_admin(q.from_user.id):
            await q.answer("只有发起人可以操作。")
            return
        if sel.airport and not self.is_admin(sel.owner.id):
            await q.answer("发起人已不是管理员，不能测速本机场订阅。", show_alert=True)
            return

        kind, _, arg = action.partition(":")
        idx = int(arg) if arg.isdigit() else -1
        if kind == "x":
            self.selections.pop(sid, None)
            sel.page = "terminated"
            await q.answer()
            await self._edit(sel.status, f"❌ 任务 <b>{esc(sel.label)}</b> 已终止。")
            self.expire(sel.status)
            return
        if kind == "u" and sel.page == "subs" and 0 <= idx < len(self.cfg.subscriptions):
            sel.sub = self.cfg.subscriptions[idx]
            sel.page = "loading"
            await q.answer()
            await self._edit(sel.status, f"📥 任务 <b>{esc(sel.label)}</b> 正在解析节点…")
            if not await self._prepare(sel):
                self.selections.pop(sid, None)
                return
            if sel.page == "terminated":  # 加载期间被终止（过期清理不算终止，菜单继续）
                return
            await self._next_step(sid, sel, context.application)
            return
        if kind == "bp" and sel.page == "backends" and idx >= 0:
            sel.backend_page = idx
            await q.answer()
            await self._render_menu(sid, sel)
            return
        if kind == "scripts" and sel.page == "tests":
            sel.page = "loading_scripts"
            await q.answer()
            if sel.scripts is None:
                try:
                    scripts = await self.api.list_scripts()
                    # 全局脚本只有元数据，由 API 解析 INTERNAL:: 引用；不能当作源码传入 configs.Scripts。
                    sel.scripts = list({s["id"]: s for s in scripts
                                        if s.get("type") == "media" and s.get("id")}.values())
                    sel.script_error = ""
                except APIError as e:
                    sel.script_error = str(e)
            if self.selections.get(sid) is not sel or sel.page != "loading_scripts":
                return  # 加载期间被取消或过期，不恢复菜单
            sel.page = "scripts"
            await self._render_menu(sid, sel)
            return
        if ((sel.page == "tests" and (kind == "clear" or kind == "t" and arg in TEST_OPTIONS))
                or (sel.page == "scripts" and (kind == "back" or kind == "sp" and idx >= 0
                                               or kind == "s" and 0 <= idx < len(sel.scripts or [])))):
            if kind == "clear":
                sel.options.clear()
                sel.script_ids.clear()
            elif kind == "t":
                sel.options.symmetric_difference_update({arg})
            elif kind == "s":
                sel.script_ids.symmetric_difference_update({sel.scripts[idx]["id"]})
            elif kind == "sp":
                sel.script_page = idx
            else:
                sel.page = "tests"
            await q.answer()
            await self._render_menu(sid, sel)
            return
        valid = ((kind == "b" and sel.page == "backends" and (arg == "auto" or 0 <= idx < len(sel.backends)))
                 or (kind == "o" and sel.page == "sort" and idx in sel.sort_choices)
                 or (kind == "go" and sel.page == "tests"))
        if not valid:
            await q.answer()
            return
        if kind == "go" and not sel.plan.matrices:
            await q.answer("请至少选择一个测试项目或流媒体脚本。", show_alert=True)
            return
        # 先检查再修改，忙碌或冷却中时菜单保持原样，稍后可以再点
        if self._busy(sel.chat_id):
            await q.answer("群里已有测速任务在运行，请等待完成后再试。", show_alert=True)
            return
        if self._cooldown_left(sel.owner.id):
            await q.answer(self._cooldown_text(sel.owner.id), show_alert=True)
            return
        if kind == "go":
            sel.tests_confirmed = True
        elif kind == "b":
            if arg == "auto":
                sel.slave = sel.slave_name = None
            else:
                sel.slave, sel.slave_name = sel.backends[idx].get("client_id"), sel.backends[idx].get("display_name")
            sel.page = "sort"
        else:
            sel.sort = SORTS[idx][1]
        if self._advance(sid, sel):
            # 忙碌检查到 _submit 预占名额之间不能 await，否则应答期间别人占满名额、菜单却已移除
            context.application.create_task(self._answer_quietly(q))
            await self._submit(sel, context.application)
        else:
            await q.answer()
            await self._render_menu(sid, sel)

    @staticmethod
    async def _answer_quietly(q) -> None:
        try:
            await q.answer()
        except TelegramError as e:
            log.debug("应答按钮失败：%s", e)

    # ------------------------------------------------------------ 提交与跟踪

    async def _submit(self, sel: Selection, application: Application, requester: str | None = None,
                      wait: bool = False, mirrors: list[Message] = ()) -> list[Message]:
        """提交测速任务。wait=True 时（自动测速）等待任务完成并返回发出的结果消息，且不检查次数、冷却和并发。
        mirrors 是其他群里同步显示进度和结果的消息（自动测速发往多个群时）。"""
        user, status = sel.owner, sel.status
        member = user is not None and not self.is_admin(user.id)
        if not wait:
            problem = None
            if user is not None and self.is_banned(user.id):
                problem = "你已被管理员禁止使用测速。"
            elif sel.airport and user is not None and not self.is_admin(user.id):
                problem = "你已不是管理员，不能测速本机场订阅。"
            elif not self.group_allowed(sel.chat_id):
                problem = "该群已不在授权名单中，无法测速。"
            elif member and self._remaining(user.id) == 0:
                problem = self._out_of_quota()
            elif member and self._cooldown_left(user.id):
                problem = self._cooldown_text(user.id)
            elif self._busy(sel.chat_id):
                problem = "群里已有测速任务在运行，请等待完成后再试。"
            if problem:
                await self._edit(status, problem)
                self.expire(status)
                return []
        # 检查之后立即（不经过 await）预占次数、冷却和群的任务名额，避免并发提交绕过限制；提交失败再退回
        stop_gen = self._stop_gen
        remaining = self.quota.consume(user.id) if member else None
        quota_day = self.quota.date  # 退回时只退同一天的扣减
        last_test = self.last_test.get(user.id) if member else None
        if member:
            self.last_test[user.id] = time.monotonic()

        def refund() -> None:
            self.quota.refund(user.id, quota_day)
            if last_test is None:
                self.last_test.pop(user.id, None)
            else:
                self.last_test[user.id] = last_test

        chats = list(dict.fromkeys([sel.chat_id, *(m.chat_id for m in mirrors)]))
        pending = f"pending-{secrets.token_hex(4)}"
        for c in chats:
            self.running.setdefault(c, set()).add(pending)
        try:
            # 菜单可能已打开好几分钟：提交前再确认一次群成员身份（在预占之后检查，期间名额不会被抢）
            if member and not wait:
                async def notify(text: str, **kw) -> None:
                    await self._edit(status, text)
                if not await self._check_member(user, sel.chat_id, sel.chat_title, application.bot, notify):
                    refund()
                    self.expire(status)
                    return []
            await self._edit(status, f"🚀 任务 <b>{esc(sel.label)}</b> 正在提交…")
            plan = sel.plan
            task_name = f"{sel.label} · {plan.title} · {user.full_name if user else '自动测速'}"[:128]
            try:
                if not sel.slave:
                    # 省略 slave_id 时 API 会返回 failed to get slave information；由 Bot 明确选择。
                    backends = await self._selectable_backends(refresh=True)
                    if not backends:
                        raise APIError("暂无可用测速后端，请稍后再试或检查后端权限设置")
                    chosen = backends[0]
                    sel.slave, sel.slave_name = chosen["client_id"], chosen.get("display_name")
                data = await self.api.submit_task(task_name, sel.nodes, list(plan.matrices),
                                                  self.cfg.task_configs(), slave_id=sel.slave)
                task_id = (data or {}).get("task_id")
                if not task_id:
                    raise APIError("API 未返回任务 ID")
            except APIError as e:
                log.warning("提交测速失败 backend=%s status=%s code=%s：%s", sel.slave, e.status, e.code, e)
                if member:
                    refund()
                for m in (status, *mirrors):
                    await self._edit(m, f"❌ 任务 <b>{esc(sel.label)}</b> 提交失败：{esc(e)}")
                    self.expire(m)
                if user is None and self.cfg.anomaly_percent > 0:
                    await self._notify_admins(application.bot,
                                              f"⚠️ 本机场订阅「{esc(sel.label)}」自动测速提交失败：{esc(e)}")
                return []
            for c in chats:
                self.running[c].add(task_id)
        finally:
            for c in chats:
                self.running[c].discard(pending)

        if user:
            self.owners[task_id] = user.id
        who = requester or "发起人 " + user.mention_html()
        self.active[task_id] = ActiveTask(task_id, sel.label, who, user.id if user else None, chats)
        if self._stop_gen != stop_gen:  # 提交过程中管理员发送了 /stopall：任务一创建就取消
            try:
                await self.api.cancel_task(task_id)
            except APIError as e:
                log.warning("取消任务 %s 失败：%s", task_id, e)
        info = f"{who} · 节点 {len(sel.nodes)} 个"
        info += f"\n测试内容：{esc(sel.test_summary[:800])}"
        if sel.name_filter:
            info += f" · 过滤 <code>{esc(sel.name_filter)}</code>"
        if remaining is not None:
            info += f" · 今日剩余 {remaining} 次"
        if sel.warnings:
            info += f"\n⚠️ {esc(sel.warnings)}"
        info += f"\nID <code>{task_id}</code>"
        sort = plan.sort if sel.sort is None else (sel.sort or None)
        view = TaskView(task_id, sel.label, info, sel.slave_name or sel.slave or "自动选择", sort,
                        auto=user is None, views=plan.views)

        if sel.chat_id != status.chat_id:
            # 私聊提交：进度和结果发到群里并 @ 发起人
            await self._edit(status, f"✅ 已提交，进度和结果将发送到群「{esc(sel.chat_title)}」。")
            try:
                status = await application.bot.send_message(sel.chat_id, self._task_text(view, "pending"),
                                                            parse_mode=ParseMode.HTML)
            except TelegramError as e:
                log.warning("向群 %s 发送任务消息失败：%s", sel.chat_id, e)
                await self._edit(sel.status, f"⚠️ 无法在群里发送消息（{esc(e)}），结果改为发到这里。")
                status = sel.status
        log.info("chat=%s user=%s 提交测速 %s（%s，%d 节点）", chats, user.id if user else "auto",
                 task_id, sel.label, len(sel.nodes))
        watch = self._watch([status, *mirrors], view, chats, application.bot)
        if wait:
            return await watch
        application.create_task(watch, name=f"watch-{task_id}")
        return []

    async def _edit(self, msg: Message, text: str, markup=None) -> None:
        # 同一消息的编辑按发起顺序发送，避免慢菜单请求晚于提交/取消提示生效。
        # 弱引用让最后一个编辑完成后自动释放锁，不积累历史消息。
        key = (msg.chat_id, msg.message_id)
        lock = self._edit_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                    disable_web_page_preview=True)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    log.warning("编辑消息失败：%s", e)
            except TelegramError as e:
                log.warning("编辑消息失败：%s", e)

    @staticmethod
    def _task_text(v: TaskView, status: str, done: int = 0, total: int = 0, backend: str | None = None,
                   extra: str = "") -> str:
        label = esc(v.label)
        head = {
            "pending": f"⏳ 任务 <b>{label}</b> 准备中…",
            "running": f"⚡ 任务 <b>{label}</b> 进行中…",
            "completed": f"✅ 任务 <b>{label}</b> 已完成",
            "failed": f"❌ 任务 <b>{label}</b> 失败",
            "canceled": f"🚫 任务 <b>{label}</b> 已取消",
        }.get(status, f"❔ 任务 <b>{label}</b> {esc(status)}")
        if status == "running":
            pct = int(100 * done / total) if total else 0
            head += f"\n<code>[{progress_bar(done, total, 16)}]</code> {pct}%  {done}/{total}"
        if extra:
            head += f"\n{extra}"
        return f"{head}\n\n后端 {esc(backend or v.backend)} · {v.info}"

    def _task_url(self, task_id: str) -> str | None:
        return self.cfg.task_url.replace("{task_id}", task_id) if self.cfg.task_url else None

    async def _share_markup(self, v: TaskView) -> InlineKeyboardMarkup | None:
        """创建公开分享（隐藏节点地址等敏感信息）并返回「查看详情」按钮。"""
        url = None
        if self.cfg.share_url:
            try:
                share = await self.api.create_share(v.task_id, v.label)
                if share and share.get("uuid"):
                    # 网页分享页使用去掉横杠的 32 位 ID，例如 share?share_id=bddb13a7…75f01
                    url = self.cfg.share_url.replace("{uuid}", str(share["uuid"]).replace("-", ""))
            except APIError as e:
                log.info("创建任务 %s 的分享失败：%s", v.task_id, e)
        url = url or self._task_url(v.task_id)
        return InlineKeyboardMarkup([[InlineKeyboardButton("📊 查看详情", url=url)]]) if url else None

    async def _watch(self, statuses: list[Message], v: TaskView, chat_ids: list[int], bot) -> list[Message]:
        """跟踪任务进度直到结束，发出结果；返回发出的结果消息。"""
        rows = [[InlineKeyboardButton("❌ 取消任务", callback_data=f"cancel:{v.task_id}")]]
        if self._task_url(v.task_id):
            rows.append([InlineKeyboardButton("📊 在 SpeedCentre+ 查看", url=self._task_url(v.task_id))])
        keyboard = InlineKeyboardMarkup(rows)
        started = time.monotonic()
        last_text = ""
        task: dict = {}
        try:
            while True:
                try:
                    task = await self.api.get_task(v.task_id)
                except APIError as e:
                    log.warning("查询任务 %s 失败：%s", v.task_id, e)
                    task = task or {}
                st = task.get("status", "pending")
                if st in FINAL_STATUSES:
                    break
                if time.monotonic() - started > self.cfg.task_timeout:
                    for m in statuses:
                        # 不删除：结果没有发出来，这条消息是用 /result 取结果的唯一线索
                        await self._edit(m, self._task_text(
                            v, st, extra=f"⌛ 等待超时，可稍后使用 /result {v.task_id} 查看结果。"))
                    if v.auto and self.cfg.anomaly_percent > 0:
                        await self._notify_admins(
                            bot, f"⌛ 本机场订阅「{esc(v.label)}」自动测速等待超时\nID <code>{v.task_id}</code>")
                    return []

                done, total = task.get("completed_nodes", 0), task.get("node_count", 0)
                if st == "running":
                    try:
                        prog = await self.api.get_progress(v.task_id)
                        done, total = prog.get("completed_count", done), prog.get("total_count", total)
                    except APIError:
                        pass
                active = self.active.get(v.task_id)
                if active:
                    active.status, active.done, active.total = st, done, total
                text = self._task_text(v, "running" if st == "running" else "pending", done, total,
                                       task.get("slave_name"))
                if text != last_text:
                    for m in statuses:
                        await self._edit(m, text, keyboard)
                    last_text = text
                await asyncio.sleep(self.cfg.poll_interval)

            st = task.get("status")
            extra = []
            if task.get("duration_ms"):
                extra.append(f"耗时 {task['duration_ms'] / 1000:.0f}s")
            if st == "failed" and task.get("error_msg"):
                extra.append(f"原因：{esc(task['error_msg'])}")
            summary = self._task_text(v, st, backend=task.get("slave_name"), extra=" · ".join(extra))
            for m in statuses:
                await self._edit(m, summary)
            done, posted, entries = statuses, [], []
            if st == "completed":
                # 只删除结果已发出的进度消息；发送失败时保留摘要（含任务 ID，可用 /result 查看）
                result = await self._send_result(statuses, v.task_id, v.sort, summary, await self._share_markup(v),
                                                 views=v.views)
                done, posted, entries = result.targets, result.messages, result.entries
            # 结果图（含同样的摘要）已发出，进度消息完成使命；失败/取消的提示同样按时删除
            for m in done:
                self.expire(m)
            await self._after_task(bot, v, task, entries)
            return posted
        except Exception:
            log.exception("跟踪任务 %s 出错", v.task_id)
            for m in statuses:
                # 同上，不删除
                await self._edit(m, self._task_text(
                    v, "unknown", extra=f"⚠️ 跟踪任务时出错，可使用 /result {v.task_id} 查看结果。"))
            return []
        finally:
            self.active.pop(v.task_id, None)
            for c in chat_ids:
                self.running.get(c, set()).discard(v.task_id)

    async def _after_task(self, bot, v: TaskView, task: dict, entries: list[dict]) -> None:
        """任务结束后：记入当天统计，必要时私聊提醒管理员（积分超过阈值、本机场节点异常）。"""
        try:
            self.stats.record(task.get("credit_cost") or 0)
            if self.stats.should_alert(self.cfg.credit_alert):
                tests, credits = self.stats.today()
                await self._notify_admins(bot, f"💰 今天已测速 {tests} 次、消耗 {credits} 积分，超过了提醒阈值 "
                                               f"{self.cfg.credit_alert}。可用 /creditalert 调整，/stopall 终止所有任务。")
            alert = self._anomaly_text(v, task, entries)
            if alert:
                await self._notify_admins(bot, alert)
        except Exception:  # 结果已经发出，统计和提醒出错不能影响它
            log.exception("任务 %s 结束后的统计或提醒出错", v.task_id)

    def _anomaly_text(self, v: TaskView, task: dict, entries: list[dict]) -> str | None:
        """本机场自动测速失败，或没有速度的节点占比达到 ANOMALY_ALERT_PERCENT 时的提醒内容。"""
        percent = self.cfg.anomaly_percent
        if not v.auto or percent <= 0:
            return None
        if task.get("status") == "failed":
            return (f"❌ 本机场订阅「{esc(v.label)}」自动测速失败：{esc(task.get('error_msg') or '未知原因')}\n"
                    f"ID <code>{v.task_id}</code>")
        if task.get("status") != "completed" or not entries:
            return None
        bad = [str((e.get("ProxyInfo") or {}).get("Name") or "?") for e in entries if node_speed(e) <= 0]
        if not bad or len(bad) * 100 < percent * len(entries):
            return None
        names = "、".join(esc(n[:30]) for n in bad[:15]) + (f" 等 {len(bad)} 个" if len(bad) > 15 else "")
        return (f"⚠️ 本机场订阅「{esc(v.label)}」自动测速异常：{len(bad)}/{len(entries)} 个节点没有速度"
                f"（提醒阈值 {percent}%）\n{names}\nID <code>{v.task_id}</code>")

    async def _send_result(self, targets: Message | list[Message], task_id: str, sort: str | None, header: str,
                           markup: InlineKeyboardMarkup | None = None,
                           views: tuple[str, ...] | None = None) -> ResultPost:
        """按测试内容发送结果视图；/result 从结果矩阵推断视图，导图失败时用对应的文字结果。"""
        targets = targets if isinstance(targets, list) else [targets]
        entries: list[dict] = []
        try:
            result = await self.api.get_result(task_id)
            entries = ((result or {}).get("result") or {}).get("Results") or []
        except APIError as e:
            log.info("获取任务 %s 结果失败：%s", task_id, e)

        stats = format_stats(entries) if entries else ""
        caption = f"{header}\n{stats}".strip()[:1000]
        geo_types = {"GEOIP_INBOUND", "GEOIP_OUTBOUND"}
        if views is None:
            types = {m.get("Type") for e in entries for m in e.get("Matrices") or []}
            views = (("normalview",) if not types or types - geo_types else ())
            if types & geo_types:
                views += ("topologyview",)
        exports = []
        for view in views:
            try:
                image = await self.api.export_image(task_id, view, sort if view == "normalview" else None)
            except APIError as e:
                log.info("导出任务 %s 的 %s 结果图失败：%s", task_id, view, e)
                image = None
            # 多视图时，文字兜底只显示当前视图的矩阵，避免重复整份结果。
            view_entries = []
            for entry in entries:
                matrices = [m for m in entry.get("Matrices") or []
                            if (m.get("Type") in geo_types) == (view == "topologyview")]
                if matrices:
                    view_entries.append({**entry, "Matrices": matrices})
            exports.append((view, image, view_entries if len(views) > 1 else entries))
        post = ResultPost([], [], entries)
        for reply_to in targets:
            complete = True
            for view, image, view_entries in exports:
                view_caption = caption
                if len(views) > 1:
                    view_caption += "\n" + ("出入口拓扑" if view == "topologyview" else "测试结果")
                try:
                    ok, sent = await self._send_result_to(reply_to, task_id, image, view_entries, view_caption, markup)
                except TelegramError as e:
                    log.warning("向 %s 发送任务 %s 的 %s 结果失败：%s", reply_to.chat_id, task_id, view, e)
                    complete = False
                    continue
                if ok:
                    if sent is not None:
                        post.messages.append(sent)
                else:
                    complete = False
                    self.expire(sent)  # “无法获取结果。”只是提示，不是测试结果
            if complete:
                post.targets.append(reply_to)
        return post

    @staticmethod
    async def _send_result_to(reply_to: Message, task_id: str, image: bytes | None, entries: list[dict],
                              caption: str, markup: InlineKeyboardMarkup | None) -> tuple[bool, Message | None]:
        """发出结果，返回 (True, 结果消息)；拿不到任何结果时只发一条提示，返回 (False, 提示)，由调用方按时删除。"""
        # 被回复的消息（命令或进度消息）可能已被定时删除，此时照常发出结果，只是不再引用
        kw = {"allow_sending_without_reply": True}
        if image:
            try:
                sent = await reply_to.reply_photo(io.BytesIO(image), caption=caption, parse_mode=ParseMode.HTML,
                                                  reply_markup=markup, **kw)
            except BadRequest as e:
                # 节点多时图片过长，Telegram 不接受为 photo，改为文件发送
                log.info("以图片发送失败（%s），改为文件发送", e)
                sent = await reply_to.reply_document(io.BytesIO(image), filename=f"{task_id}.png", caption=caption,
                                                     parse_mode=ParseMode.HTML, reply_markup=markup, **kw)
            return True, sent
        if not entries:
            return False, await reply_to.reply_text(f"{caption}\n\n无法获取结果。", parse_mode=ParseMode.HTML,
                                                    reply_markup=markup, **kw)
        # 按行切分，避免截断 HTML 标签
        chunks = _split(f"{caption}\n\n{format_result_text(entries)}", 4000)
        first = None
        for i, chunk in enumerate(chunks):
            sent = await reply_to.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                             reply_markup=markup if i == len(chunks) - 1 else None, **kw)
            first = first or sent
        return True, first


def _split(text: str, size: int) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def main() -> None:
    cfg = Config.from_env()
    bot = SpeedBot(cfg)
    scheduler_task: list[asyncio.Task | None] = [None]

    async def _post_init(app: Application) -> None:
        await bot.set_commands(app.bot)
        # post_init 时 Application 还没进入运行状态，用 asyncio 直接创建定时任务，关闭时再取消
        scheduler_task[0] = asyncio.create_task(bot.scheduler(app), name="auto-speedtest")

    async def _post_stop(app: Application) -> None:
        await bot.flush_expiring()

    async def _post_shutdown(app: Application) -> None:
        if scheduler_task[0]:
            scheduler_task[0].cancel()
        await bot.api.close()

    # 并发处理更新：一个用户的订阅拉取或 API 调用较慢时，不影响其他人（包括删除群里的订阅链接）
    app = (Application.builder().token(cfg.bot_token).concurrent_updates(True)
           .post_init(_post_init).post_stop(_post_stop).post_shutdown(_post_shutdown).build())
    # 先于其他处理器检查群消息中的订阅链接
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION),
                                   bot.on_group_message), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.PINNED_MESSAGE, bot.on_pinned))
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("id", bot.cmd_id))
    for name, _ in USER_COMMANDS + ADMIN_COMMANDS:
        app.add_handler(CommandHandler(name, getattr(bot, f"cmd_{name}")))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, bot.on_private_text))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^(cancel|sel|group):"))
    log.info("Bot 启动，本机场订阅：%s，自动测速：%s，授权群组：%s，每日次数：%s",
             "、".join(n for n, _ in cfg.subscriptions) or "无",
             bot.schedule.describe() if bot.schedule else "未启用",
             cfg.allowed_chat_ids or "不限", cfg.daily_limit if cfg.daily_limit > 0 else "不限")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
