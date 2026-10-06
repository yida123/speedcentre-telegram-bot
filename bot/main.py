"""SpeedCentre+ Telegram 群组测速 Bot。

- 每天定时自动测速本机场的固定订阅（subscriptions.yaml），结果发到群里；管理员可随时手动触发。
- 群成员可以私聊发送任意订阅链接测速（每人每天限次），结果发回群里并 @ 发起人。
"""
import asyncio
import io
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
    filters,
)

from .api import APIError, SCPClient
from .config import Config
from .formatter import PRESETS, build_plan, esc, format_result_text, format_stats, progress_bar
from .quota import DailyQuota
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
NOTICE_TTL = 120  # 群内引导消息自动删除时间（秒）
SUB_FETCH_TIMEOUT = 60  # 拉取一次提交中所有订阅的总超时（秒），防止慢速服务器拖住 bot
MAX_SUB_URLS = 5  # 一次最多拉取的订阅链接数
MAX_FILTER_LEN = 64
MEMBER_LABEL = "群友订阅"  # 群成员私聊提交的订阅在群里显示的名称
SPEED_PLAN = build_plan(PRESETS["speed"].title, PRESETS["speed"].options)
# 结果图排序方式（按钮文字, export 的 sort 参数；空字符串为订阅原顺序）
SORTS = [
    ("📋 订阅顺序（默认）", ""),
    ("🀄 节点名（升序）", "name_asc"),
    ("🚀 平均速度（升序）", "avg_speed_asc"),
    ("🚀 平均速度（降序）", "avg_speed_desc"),
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
    """测速提交前的选择状态（订阅 → 后端 → 排序）。"""
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
    sort: str | None = None  # None 表示未选择（使用平均速度降序）
    warnings: str = ""  # 部分订阅失败或被跳过时的提示
    page: str = "subs"
    created: float = field(default_factory=time.monotonic)

    @property
    def label(self) -> str:
        return self.sub[0] if self.sub else "-"


@dataclass
class TaskView:
    """已提交任务的展示信息。"""
    task_id: str
    label: str  # 订阅名
    info: str  # 发起人、节点数、任务 ID 等
    backend: str  # 选择的后端（自动选择时由任务状态中的 slave_name 覆盖）
    sort: str | None


HELP_TEXT = """<b>机场节点测速 Bot</b>

<b>测自己的订阅</b>：在群里发送 <code>/speed</code>，点击「🔒 私聊发送订阅」，在私聊里发送订阅链接，
选择测试后端和排序方式后开始，测速进度和结果图会发回群里并 @ 你。{quota}
为防止泄露，群里出现的订阅链接会被自动删除。

<b>本机场节点状态</b>：{schedule}管理员可发送 <code>/speed</code> 或 <code>/autotest</code> 手动测速。

<b>命令</b>：
/speed — 测速
/sub — 本机场订阅、自动测速时间和你的剩余次数
/backends — 测试后端列表
/result 任务ID — 重新获取结果图

<b>可选参数</b>（跟在链接后面）：
<code>-f 关键词</code> 只测名称包含关键词的节点，多个用 | 分隔，例如 <code>-f "香港|HK"</code>
<code>-s 后端ID或名称</code> 直接指定测试后端"""


class SpeedBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = SCPClient(cfg.api_key, cfg.api_base)
        self.quota = DailyQuota(os.path.join(cfg.data_dir, "usage.json"), cfg.daily_limit, cfg.timezone)
        self.tz = ZoneInfo(cfg.timezone)
        self.running: dict[int, set[str]] = {}  # chat_id -> task_ids
        self.owners: dict[str, int] = {}  # task_id -> user_id
        self.selections: dict[str, Selection] = {}  # 菜单 id -> 选择状态
        self.dm_targets: dict[int, DMTarget] = {}  # user_id -> 私聊提交的结果去向
        self._backends: tuple[float, list[dict]] | None = None
        self._auto_lock = asyncio.Lock()
        self._loading: set[int] = set()  # 正在解析订阅的群成员，每人同时只能有一个
        self._parsing: dict[int, int] = {}  # 群成员仍在后台线程里跑的解析数（超时后线程不会停，跑完才算结束）
        self._timers: set[asyncio.Task] = set()  # 待执行的定时删除

    # ------------------------------------------------------------ 群消息自动删除

    def expire(self, msg: Message | None, delay: float | None = None) -> None:
        """群里除测速结果外的消息（提示、菜单、进度、用户的命令）在 AUTO_DELETE_SECONDS 秒后删除。
        私聊消息不删（Telegram 中群聊 ID 为负数、私聊为正数）。"""
        delay = self.cfg.auto_delete_seconds if delay is None else delay
        if msg is None or delay <= 0 or msg.chat_id > 0:
            return
        task = asyncio.get_running_loop().create_task(self._delete_later(msg, delay))
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _say(self, msg: Message, text: str, **kw) -> Message:
        """回复一条临时消息（在群里会按 AUTO_DELETE_SECONDS 自动删除）。"""
        sent = await msg.reply_text(text, **kw)
        self.expire(sent)
        return sent

    # ------------------------------------------------------------ 权限与次数

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.cfg.admin_user_ids

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
        if not self.cfg.schedule_times or not self.cfg.subscriptions:
            return ""
        times = "、".join(f"{h:02d}:{m:02d}" for h, m in self.cfg.schedule_times)
        return f"每天 {times} 自动测速并发到群里，"

    # ------------------------------------------------------------ 基础命令

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.expire(update.effective_message)
        quota = f"每人每天 {self.cfg.daily_limit} 次。" if self.cfg.daily_limit > 0 else ""
        await self._say(update.effective_message, 
            HELP_TEXT.format(quota=quota, schedule=self._schedule_text()),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)

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
        texts = [" ".join(context.args or [])]
        if msg.reply_to_message:
            texts.append(msg.reply_to_message.text or msg.reply_to_message.caption or "")
        task_id = next((m.group(0) for m in map(UUID_RE.search, texts) if m), None)
        if not task_id:
            await self._say(msg, "用法：/result 任务ID，或回复任务消息。")
            return
        await self._send_result(msg, task_id, "avg_speed_desc", f"任务 <code>{task_id}</code>")

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
            sel = Selection(owner=user, chat_id=chat.id, status=status, name_filter=name_filter, slave_arg=slave)
            self.selections[sid] = sel
            await self._render_menu(sid, sel)
            return
        status = await msg.reply_text(f"📥 任务 <b>{esc(sub[0])}</b> 正在解析节点…", parse_mode=ParseMode.HTML)
        sel = Selection(owner=user, chat_id=chat.id, status=status, name_filter=name_filter, slave_arg=slave, sub=sub)
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
        if update.effective_chat.type != ChatType.PRIVATE or not m:
            await self.cmd_help(update, context)
            return
        chat_id = int(m.group(1))
        if not self.group_allowed(chat_id):
            await self._say(msg, "该群未授权使用此 Bot。")
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
            await self._say(msg, "请发送订阅链接或节点链接。发送 /help 查看用法。")
            return
        await self._private_speed(update, context, (msg.text or "").split())

    async def _resolve_target(self, user: User, bot) -> DMTarget | None:
        """私聊提交的结果发到哪个群：最近点过按钮的群 > 唯一授权群（需是群成员）。"""
        target = self.dm_targets.get(user.id)
        if target and not target.expired():
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
        target = await self._resolve_target(user, context.bot)
        if not target:
            await self._say(msg, "请先在机场群里发送 /speed，然后点击「🔒 私聊发送订阅」按钮。")
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
        try:
            # 节点很多时转换也比较耗时，放到线程里
            sel.nodes, sel.skipped = await asyncio.to_thread(
                to_api_nodes, proxies, sel.name_filter, self.cfg.max_nodes)
        except Exception:
            log.warning("转换节点出错", exc_info=True)
            sel.nodes, sel.skipped = [], len(proxies)
            errors.append("节点内容无法解析")
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
        """决定下一步：需要选后端/排序时切换页面并返回 False；可以提交时移除菜单并返回 True。
        这里不 await，保证同一菜单被并发点击时只会提交一次。"""
        if self.cfg.backend_select and not sel.slave_arg and len(sel.backends) > 1 and sel.page != "sort":
            sel.page = "backends"
        elif self.cfg.sort_select and sel.sort is None:
            sel.page = "sort"
        else:
            self.selections.pop(sid, None)
            sel.page = "submitting"
            return True
        self.selections[sid] = sel
        return False

    async def _next_step(self, sid: str, sel: Selection, application: Application) -> None:
        """节点就绪后：按需选后端 → 选排序 → 提交。"""
        if self._advance(sid, sel):
            await self._submit(sel, application)
        else:
            await self._render_menu(sid, sel)

    # ------------------------------------------------------------ 每日自动测速

    def seconds_until_next_run(self, now: datetime | None = None) -> float | None:
        """距离下一个 SCHEDULE_TIMES 时间点的秒数；未配置时返回 None。"""
        if not self.cfg.schedule_times:
            return None
        now = now or datetime.now(self.tz)
        # 同一 tzinfo 的 aware datetime 比较和相减都按墙上时间、忽略 UTC 偏移，全部换成 UTC，夏令时切换日也准确
        now_utc = now.astimezone(timezone.utc)
        candidates = []
        for days in (0, 1):
            day = (now + timedelta(days=days)).date()
            for h, m in self.cfg.schedule_times:
                t = datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz).astimezone(timezone.utc)
                if t > now_utc:
                    candidates.append(t)
        return (min(candidates) - now_utc).total_seconds()

    def _auto_chats(self) -> list[int]:
        return sorted(self.cfg.auto_chat_ids or self.cfg.allowed_chat_ids)

    async def scheduler(self, application: Application) -> None:
        """按 SCHEDULE_TIMES 每天自动测速本机场订阅并发到群里。"""
        if not self.cfg.subscriptions or not self._auto_chats():
            log.info("未配置本机场订阅或目标群，不启用每日自动测速")
            return
        while True:
            delay = self.seconds_until_next_run()
            if delay is None:
                return
            log.info("下次自动测速在 %.0f 秒后", delay)
            await asyncio.sleep(delay)
            try:
                # 管理员手动测速正在进行时，等它结束后再跑，不跳过当天的定时测速
                await self.run_auto(application, self._auto_chats(), "🕘 每日自动测速", wait_for_lock=True)
            except Exception:
                log.exception("每日自动测速出错")
            await asyncio.sleep(1)  # 避免同一分钟内重复触发

    async def run_auto(self, application: Application, chat_ids: list[int], title: str,
                       wait_for_lock: bool = False) -> bool:
        """依次测速所有本机场订阅（每个订阅只提交一次，结果同时发到所有目标群），每个等上一个完成后再开始。
        已有自动测速在进行且 wait_for_lock=False 时返回 False。"""
        if self._auto_lock.locked() and not wait_for_lock:
            return False
        async with self._auto_lock:
            for sub in self.cfg.subscriptions:
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
                if error:
                    for m in statuses:
                        await self._edit(m, error)
                        self.expire(m)
                    continue
                if self.cfg.auto_slave_id:
                    chosen = self._match_backend(sel.backends, self.cfg.auto_slave_id)
                    if chosen:
                        sel.slave, sel.slave_name = chosen["client_id"], chosen.get("display_name")
                await self._submit(sel, application, requester=title, wait=True, mirrors=statuses[1:])
        return True

    async def cmd_autotest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """管理员手动触发一次本机场订阅测速（与每日自动测速相同）。"""
        if not await self.guard(update):
            return
        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if not self.is_admin(user.id):
            await self._say(msg, "只有管理员可以手动触发本机场测速。")
            return
        if not self.cfg.subscriptions:
            await self._say(msg, "还没有配置本机场订阅（subscriptions.yaml）。")
            return
        if self._auto_lock.locked():
            await self._say(msg, "本机场测速正在进行中，请等待完成。")
            return
        names = "、".join(n for n, _ in self.cfg.subscriptions)
        await self._say(msg, f"开始测速本机场订阅：{esc(names)}", parse_mode=ParseMode.HTML)
        context.application.create_task(
            self.run_auto(context.application, [chat.id], f"🛠 管理员 {user.mention_html()} 手动测速"))

    # ------------------------------------------------------------ 选择菜单

    def _purge_selections(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self.selections.items() if now - s.created > SELECTION_TTL]:
            sel = self.selections.pop(sid, None)
            if sel:
                self.expire(sel.status, 0.1)  # 过期没人点的菜单直接清理

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
        if b.get("allow_public_access") is False:
            return False
        return not self.cfg.allowed_backends or b.get("client_id") in self.cfg.allowed_backends

    async def _selectable_backends(self) -> list[dict]:
        """可选后端：在线、允许 Copilot 调用、在 ALLOWED_BACKENDS 内，保持 API 返回的顺序。"""
        return [b for b in await self._all_backends() if b.get("is_online") and self._backend_allowed(b)]

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
        def btn(label: str, action: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(label, callback_data=f"sel:{sid}:{action}")

        rows: list[list[InlineKeyboardButton]] = []
        who = sel.owner.mention_html() if sel.owner else ""
        if sel.page == "subs":
            text = f"📋 <b>选择要测速的订阅</b> · {who}"
            buttons = [btn(name[:30], f"u:{n}") for n, (name, _) in enumerate(self.cfg.subscriptions)]
            rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
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
            buttons = [btn(label, f"o:{n}") for n, (label, _) in enumerate(SORTS)]
            rows += [buttons[:1], buttons[1:2], buttons[2:]]
        if sel.warnings and sel.page in ("backends", "sort"):
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
            await q.answer("选择已过期，请重新发送 /speed。", show_alert=True)
            return
        if q.from_user.id != sel.owner.id and not self.is_admin(q.from_user.id):
            await q.answer("只有发起人可以操作。")
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
        valid = ((kind == "b" and sel.page == "backends" and (arg == "auto" or 0 <= idx < len(sel.backends)))
                 or (kind == "o" and sel.page == "sort" and 0 <= idx < len(SORTS)))
        if not valid:
            await q.answer()
            return
        # 先检查再修改，忙碌时菜单保持原样，稍后可以再点
        if self._busy(sel.chat_id):
            await q.answer("群里已有测速任务在运行，请等待完成后再试。", show_alert=True)
            return
        if kind == "b":
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
                      wait: bool = False, mirrors: list[Message] = ()) -> None:
        """提交测速任务。wait=True 时（自动测速）等待任务完成，且不检查次数和并发。
        mirrors 是其他群里同步显示进度和结果的消息（自动测速发往多个群时）。"""
        user, status = sel.owner, sel.status
        member = user is not None and not self.is_admin(user.id)
        if not wait:
            if member and self._remaining(user.id) == 0:
                await self._edit(status, self._out_of_quota())
                self.expire(status)
                return
            if self._busy(sel.chat_id):
                await self._edit(status, "群里已有测速任务在运行，请等待完成后再试。")
                self.expire(status)
                return
        # 检查之后立即（不经过 await）预占次数和群的任务名额，避免并发提交绕过限制；提交失败再退回
        remaining = self.quota.consume(user.id) if member else None
        quota_day = self.quota.date  # 退回时只退同一天的扣减
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
                    self.quota.refund(user.id, quota_day)
                    self.expire(status)
                    return
            await self._edit(status, f"🚀 任务 <b>{esc(sel.label)}</b> 正在提交…")
            task_name = f"{sel.label} · 测速 · {user.full_name if user else '自动测速'}"[:128]
            try:
                data = await self.api.submit_task(task_name, sel.nodes, list(SPEED_PLAN.matrices),
                                                  self.cfg.task_configs(), slave_id=sel.slave)
                task_id = (data or {}).get("task_id")
                if not task_id:
                    raise APIError("API 未返回任务 ID")
            except APIError as e:
                if member:
                    self.quota.refund(user.id, quota_day)
                for m in (status, *mirrors):
                    await self._edit(m, f"❌ 任务 <b>{esc(sel.label)}</b> 提交失败：{esc(e)}")
                    self.expire(m)
                return
            for c in chats:
                self.running[c].add(task_id)
        finally:
            for c in chats:
                self.running[c].discard(pending)

        if user:
            self.owners[task_id] = user.id
        info = f"{requester or '发起人 ' + user.mention_html()} · 节点 {len(sel.nodes)} 个"
        if sel.name_filter:
            info += f" · 过滤 <code>{esc(sel.name_filter)}</code>"
        if remaining is not None:
            info += f" · 今日剩余 {remaining} 次"
        if sel.warnings:
            info += f"\n⚠️ {esc(sel.warnings)}"
        info += f"\nID <code>{task_id}</code>"
        sort = "avg_speed_desc" if sel.sort is None else (sel.sort or None)
        view = TaskView(task_id, sel.label, info, sel.slave_name or sel.slave or "自动选择", sort)

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
        watch = self._watch([status, *mirrors], view, chats)
        if wait:
            await watch
        else:
            application.create_task(watch, name=f"watch-{task_id}")

    async def _edit(self, msg: Message, text: str, markup=None) -> None:
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

    async def _watch(self, statuses: list[Message], v: TaskView, chat_ids: list[int]) -> None:
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
                        await self._edit(m, self._task_text(
                            v, st, extra=f"⌛ 等待超时，可稍后使用 /result {v.task_id} 查看结果。"))
                        self.expire(m)
                    return

                done, total = task.get("completed_nodes", 0), task.get("node_count", 0)
                if st == "running":
                    try:
                        prog = await self.api.get_progress(v.task_id)
                        done, total = prog.get("completed_count", done), prog.get("total_count", total)
                    except APIError:
                        pass
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
            if st == "completed":
                await self._send_result(statuses, v.task_id, v.sort, summary, await self._share_markup(v))
            # 结果图（含同样的摘要）已发出，进度消息完成使命；失败/取消的提示同样按时删除
            for m in statuses:
                self.expire(m)
        except Exception:
            log.exception("跟踪任务 %s 出错", v.task_id)
            for m in statuses:
                await self._edit(m, self._task_text(
                    v, "unknown", extra=f"⚠️ 跟踪任务时出错，可使用 /result {v.task_id} 查看结果。"))
                self.expire(m)
        finally:
            for c in chat_ids:
                self.running.get(c, set()).discard(v.task_id)

    async def _send_result(self, targets: Message | list[Message], task_id: str, sort: str | None, header: str,
                           markup: InlineKeyboardMarkup | None = None) -> None:
        """把结果图（导不出图时为文字结果）发到每个目标消息下面；结果和图片只获取一次。"""
        targets = targets if isinstance(targets, list) else [targets]
        entries: list[dict] = []
        try:
            result = await self.api.get_result(task_id)
            entries = ((result or {}).get("result") or {}).get("Results") or []
        except APIError as e:
            log.info("获取任务 %s 结果失败：%s", task_id, e)

        stats = format_stats(entries) if entries else ""
        caption = f"{header}\n{stats}".strip()[:1000]
        try:
            image = await self.api.export_image(task_id, "normalview", sort)
        except APIError as e:
            log.info("导出任务 %s 结果图失败：%s", task_id, e)
            image = None
        for reply_to in targets:
            try:
                await self._send_result_to(reply_to, task_id, image, entries, caption, markup)
            except TelegramError as e:
                log.warning("向 %s 发送任务 %s 结果失败：%s", reply_to.chat_id, task_id, e)

    @staticmethod
    async def _send_result_to(reply_to: Message, task_id: str, image: bytes | None, entries: list[dict],
                              caption: str, markup: InlineKeyboardMarkup | None) -> None:
        # 被回复的消息（命令或进度消息）可能已被定时删除，此时照常发出结果，只是不再引用
        kw = {"allow_sending_without_reply": True}
        if image:
            try:
                await reply_to.reply_photo(io.BytesIO(image), caption=caption, parse_mode=ParseMode.HTML,
                                           reply_markup=markup, **kw)
            except BadRequest as e:
                # 节点多时图片过长，Telegram 不接受为 photo，改为文件发送
                log.info("以图片发送失败（%s），改为文件发送", e)
                await reply_to.reply_document(io.BytesIO(image), filename=f"{task_id}.png", caption=caption,
                                              parse_mode=ParseMode.HTML, reply_markup=markup, **kw)
            return
        if not entries:
            await reply_to.reply_text(f"{caption}\n\n无法获取结果。", parse_mode=ParseMode.HTML, reply_markup=markup, **kw)
            return
        # 按行切分，避免截断 HTML 标签
        chunks = _split(f"{caption}\n\n{format_result_text(entries)}", 4000)
        for i, chunk in enumerate(chunks):
            await reply_to.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                      reply_markup=markup if i == len(chunks) - 1 else None, **kw)


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
        await app.bot.set_my_commands([
            BotCommand("speed", "测速（私聊发送订阅）"),
            BotCommand("sub", "本机场订阅与剩余次数"),
            BotCommand("backends", "测试后端列表"),
            BotCommand("help", "帮助"),
        ])
        # post_init 时 Application 还没进入运行状态，用 asyncio 直接创建定时任务，关闭时再取消
        scheduler_task[0] = asyncio.create_task(bot.scheduler(app), name="daily-speedtest")

    async def _post_shutdown(app: Application) -> None:
        if scheduler_task[0]:
            scheduler_task[0].cancel()
        await bot.api.close()

    # 并发处理更新：一个用户的订阅拉取或 API 调用较慢时，不影响其他人（包括删除群里的订阅链接）
    app = (Application.builder().token(cfg.bot_token).concurrent_updates(True)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    # 先于其他处理器检查群消息中的订阅链接
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION),
                                   bot.on_group_message), group=-1)
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("id", bot.cmd_id))
    app.add_handler(CommandHandler("speed", bot.cmd_speed))
    app.add_handler(CommandHandler("autotest", bot.cmd_autotest))
    app.add_handler(CommandHandler("sub", bot.cmd_sub))
    app.add_handler(CommandHandler("backends", bot.cmd_backends))
    app.add_handler(CommandHandler("result", bot.cmd_result))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, bot.on_private_text))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^(cancel|sel):"))
    log.info("Bot 启动，本机场订阅：%s，自动测速：%s，授权群组：%s，每日次数：%s",
             "、".join(n for n, _ in cfg.subscriptions) or "无",
             "、".join(f"{h:02d}:{m:02d}" for h, m in cfg.schedule_times) or "未启用",
             cfg.allowed_chat_ids or "不限", cfg.daily_limit if cfg.daily_limit > 0 else "不限")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
