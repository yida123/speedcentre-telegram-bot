"""SpeedCentre+ Telegram 群组测试 Bot。"""
import asyncio
import io
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
    filters,
)

from .api import APIError, SCPClient
from .config import Config
from .formatter import (
    PRESETS, STATUS_TEXT, TEST_OPTIONS, TestPlan, build_plan, esc, format_result_text, format_stats,
    progress_bar,
)
from .subscription import (
    DEFAULT_SUB_LINK_PATTERN, SubscriptionError, contains_sensitive_link, extract_sources, fetch_subscription,
    parse_uri, to_api_nodes,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("speed_bot")

FINAL_STATUSES = {"completed", "failed", "canceled"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

SELECTION_TTL = 600  # 测试项选择菜单的有效期（秒）
BACKENDS_PER_PAGE = 8
NOTICE_TTL = 60  # 群内私聊引导消息自动删除时间（秒）


@dataclass
class DMTarget:
    """私聊提交的测试结果要发往的会话。"""
    chat_id: int
    title: str
    command: str
    updated: float = field(default_factory=time.monotonic)

    def expired(self, ttl: float) -> bool:
        return time.monotonic() - self.updated > ttl

    def touch(self) -> None:
        self.updated = time.monotonic()


@dataclass
class Selection:
    """/test 解析完节点后、等待用户选择测试项时的状态。"""
    owner: User
    chat_id: int  # 结果发往的会话（通常是群）
    target_title: str
    status: Message  # 私聊里的菜单/状态消息
    nodes: list[dict]
    skipped: int
    name_filter: str | None
    slave: str | None  # 选定的后端 ID，None 表示自动选择
    options: set[str]
    slave_name: str | None = None
    backends: list[dict] = field(default_factory=list)  # 可选后端快照
    backend_page: int = 0
    quick: str | None = None  # 快捷命令：选完后端直接按该预设开测
    scripts: set[str] = field(default_factory=set)
    script_list: list[tuple[str, str]] = field(default_factory=list)
    page: str = "main"
    created: float = field(default_factory=time.monotonic)


HELP_TEXT = """<b>SpeedCentre+ 测试 Bot</b>

<b>使用方式</b>：在群里发送测试命令，点击「私聊发送订阅」按钮，在私聊中发送订阅链接或节点链接。
测试进度和结果会发回群里并 @ 你。为防止泄露，群里出现的订阅链接会被自动删除。

<b>测试命令</b>：
/test — 弹出菜单，自选测试项目（延迟、测速、UDP、拓扑、劫持检测、流媒体解锁…）和后端
/speed — 测速
/ping — 延迟测试（RTT / HTTPS / 丢包）
/udp — UDP NAT 类型
/topo — 入口/出口拓扑分析

<b>可选参数</b>：
<code>-f 正则</code> 按节点名过滤，例如 <code>-f "香港|HK"</code>
<code>-s 后端ID或名称</code> 直接指定后端（见 /backends），不指定时会弹出后端选择按钮

<b>其他</b>：
/backends — 后端列表
/tasks — 最近任务
/result 任务ID — 重新获取结果图
/cancel 任务ID — 取消任务（也可点击按钮）

私聊示例：<code>/test https://example.com/sub -f 香港</code>，或直接发送链接"""


class SpeedBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = SCPClient(cfg.api_key, cfg.api_base)
        self.running: dict[int, set[str]] = {}  # chat_id -> task_ids
        self.owners: dict[str, int] = {}  # task_id -> user_id
        self.selections: dict[str, Selection] = {}  # 菜单 id -> 选择状态
        self.dm_targets: dict[int, DMTarget] = {}  # user_id -> 私聊提交的结果去向
        self._scripts: tuple[float, list[tuple[str, str]]] | None = None
        self._backends: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------ 权限

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.cfg.admin_user_ids

    def allowed(self, update: Update) -> bool:
        chat, user = update.effective_chat, update.effective_user
        if self.is_admin(user and user.id):
            return True
        if chat.type == ChatType.PRIVATE:
            return self.cfg.allow_private
        return not self.cfg.allowed_chat_ids or chat.id in self.cfg.allowed_chat_ids

    async def guard(self, update: Update) -> bool:
        if self.allowed(update):
            return True
        chat = update.effective_chat
        log.info("拒绝来自 chat=%s user=%s 的请求", chat.id, update.effective_user and update.effective_user.id)
        if chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text("此 Bot 仅限授权群组使用。")
        else:
            await update.effective_message.reply_text(f"本群未授权使用此 Bot。群组 ID：<code>{chat.id}</code>",
                                                      parse_mode=ParseMode.HTML)
        return False

    # ------------------------------------------------------------ 基础命令

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            f"群组 ID：<code>{update.effective_chat.id}</code>\n用户 ID：<code>{update.effective_user.id}</code>",
            parse_mode=ParseMode.HTML)

    async def cmd_backends(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        try:
            backends = await self.api.list_backends()
        except APIError as e:
            await update.effective_message.reply_text(f"获取后端失败：{esc(e)}")
            return
        if not backends:
            await update.effective_message.reply_text("暂无可用后端。")
            return
        backends.sort(key=lambda b: (not b.get("is_online"), b.get("display_name") or ""))
        lines = ["<b>后端列表</b>（🟢 在线 · 🔴 离线 · 🚫 不可选）"]
        for b in backends:
            state = "🟢" if b.get("is_online") else "🔴"
            if not self._backend_allowed(b):
                state = "🚫"
            lines.append(
                f"{state} <b>{esc(b.get('display_name') or '-')}</b>\n"
                f"    ID <code>{esc(b.get('client_id'))}</code> · 队列 连接 {b.get('conn_pending', 0)} / "
                f"测速 {b.get('speed_pending', 0)}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        try:
            data = await self.api.list_tasks(page_size=10)
        except APIError as e:
            await update.effective_message.reply_text(f"获取任务失败：{esc(e)}")
            return
        items = data.get("items") or []
        if not items:
            await update.effective_message.reply_text("暂无任务。")
            return
        lines = [f"<b>最近任务</b>（共 {data.get('total', len(items))} 个）"]
        for t in items:
            lines.append(
                f"{STATUS_TEXT.get(t.get('status'), t.get('status'))} {esc(t.get('name', ''))}\n"
                f"    <code>{t.get('task_id')}</code> · {t.get('completed_nodes', 0)}/{t.get('node_count', 0)} 节点"
                f" · {t.get('credit_cost', 0)} 积分"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        task_id = self._task_id_from(update.effective_message, context.args)
        if not task_id:
            await update.effective_message.reply_text("用法：/cancel 任务ID，或回复任务消息。")
            return
        await update.effective_message.reply_text(await self._cancel(task_id, update.effective_user.id))

    async def cmd_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        task_id = self._task_id_from(update.effective_message, context.args)
        if not task_id:
            await update.effective_message.reply_text("用法：/result 任务ID，或回复任务消息。")
            return
        views = ("topologyview",) if "topo" in (context.args or []) else ("normalview",)
        await self._send_result(update.effective_message, task_id, views, "avg_speed_desc", "")

    @staticmethod
    def _task_id_from(msg: Message, args: list[str] | None) -> str | None:
        texts = [" ".join(args or [])]
        if msg.reply_to_message:
            texts.append(msg.reply_to_message.text or msg.reply_to_message.caption or "")
        for t in texts:
            m = UUID_RE.search(t)
            if m:
                return m.group(0)
        return None

    async def _cancel(self, task_id: str, user_id: int) -> str:
        owner = self.owners.get(task_id)
        if owner is not None and owner != user_id and not self.is_admin(user_id):
            return "只有任务发起人或管理员可以取消该任务。"
        if owner is None and not self.is_admin(user_id):
            return "只有管理员可以取消非本 Bot 会话发起的任务。"
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

    # ------------------------------------------------------------ 测试命令

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

    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, chat = update.effective_message, update.effective_chat
        command = (msg.text or "").split()[0].lstrip("/").split("@")[0].lower()
        if command not in PRESETS:
            command = "test"
        if chat.type == ChatType.PRIVATE:
            await self._private_test(update, context, command, context.args or [])
            return
        if not await self.guard(update):
            return
        # 群里不接收订阅，引导发起人私聊提交，结果再发回本群
        deleted = False
        if self.cfg.delete_sub_message and any(extract_sources(msg.text or "")):
            deleted = await self._delete(msg)
        await self._send_dm_prompt(msg, update.effective_user, chat.id, command, context, deleted=deleted)

    # ------------------------------------------------------------ 群内订阅保护与私聊引导

    def _dm_markup(self, bot_username: str, chat_id: int, command: str) -> InlineKeyboardMarkup:
        url = f"https://t.me/{bot_username}?start=g{chat_id}_{command}"
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔒 私聊发送订阅", url=url)]])

    async def _send_dm_prompt(self, msg: Message, user: User, chat_id: int, command: str,
                              context: ContextTypes.DEFAULT_TYPE, deleted: bool = False) -> None:
        title = PRESETS[command].title if command != "test" else "测试"
        text = (f"{user.mention_html()} " + ("已删除你发送的订阅/节点链接，避免泄露。\n" if deleted else "")
                + f"请点击下方按钮，在私聊中发送订阅链接发起{title}，结果会发回本群。")
        markup = self._dm_markup(context.bot.username, chat_id, command)
        try:
            if deleted:
                notice = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                notice = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError as e:
            log.warning("发送私聊引导失败：%s", e)
            return
        context.application.create_task(self._delete_later(notice, NOTICE_TTL))

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
        await self._delete(msg)

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """授权群内出现节点链接或疑似订阅链接时立即删除，并引导发送者私聊。"""
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not msg or not self.cfg.delete_sub_message or not user or user.is_bot:
            return
        if self.cfg.allowed_chat_ids and chat.id not in self.cfg.allowed_chat_ids:
            return
        pattern = self.cfg.sub_link_pattern or DEFAULT_SUB_LINK_PATTERN
        if not contains_sensitive_link(msg.text or msg.caption or "", pattern):
            return
        log.info("删除群 %s 中用户 %s 发送的订阅链接", chat.id, user.id)
        deleted = await self._delete(msg)
        await self._send_dm_prompt(msg, user, chat.id, "test", context, deleted=deleted)
        raise ApplicationHandlerStop

    async def _is_member(self, bot, chat_id: int, user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramError:
            return False
        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)

    async def _chat_title(self, bot, chat_id: int) -> str:
        try:
            return (await bot.get_chat(chat_id)).title or str(chat_id)
        except TelegramError:
            return str(chat_id)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        payload = (context.args or [""])[0]
        m = re.fullmatch(r"g(-?\d+)_(\w+)", payload)
        if update.effective_chat.type != ChatType.PRIVATE or not m:
            await self.cmd_help(update, context)
            return
        chat_id, command = int(m.group(1)), m.group(2) if m.group(2) in PRESETS else "test"
        if self.cfg.allowed_chat_ids and chat_id not in self.cfg.allowed_chat_ids:
            await msg.reply_text("该群未授权使用此 Bot。")
            return
        if not self.is_admin(user.id) and not await self._is_member(context.bot, chat_id, user.id):
            await msg.reply_text("你不是该群成员，无法为该群发起测试。")
            return
        title = await self._chat_title(context.bot, chat_id)
        self.dm_targets[user.id] = DMTarget(chat_id, title, command)
        await msg.reply_text(
            f"好的，结果将发送到群「{esc(title)}」。\n\n请直接发送订阅链接或节点链接"
            f"（可附加 <code>-f 正则</code> 过滤节点、<code>-s 后端ID</code> 指定后端）。",
            parse_mode=ParseMode.HTML)

    async def on_private_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not any(extract_sources(msg.text or "")):
            await msg.reply_text("请发送订阅链接或节点链接。发送 /help 查看用法。")
            return
        target = self.dm_targets.get(update.effective_user.id)
        command = target.command if target and not target.expired(self.cfg.dm_target_ttl) else "test"
        await self._private_test(update, context, command, (msg.text or "").split())

    async def _resolve_target(self, user: User, bot) -> DMTarget | None:
        """私聊提交的结果发到哪里：最近点过按钮的群 > 唯一授权群 > 私聊本身（管理员或 ALLOW_PRIVATE）。"""
        target = self.dm_targets.get(user.id)
        if target and not target.expired(self.cfg.dm_target_ttl):
            return target
        if len(self.cfg.allowed_chat_ids) == 1:
            chat_id = next(iter(self.cfg.allowed_chat_ids))
            if self.is_admin(user.id) or await self._is_member(bot, chat_id, user.id):
                target = DMTarget(chat_id, await self._chat_title(bot, chat_id), "test")
                self.dm_targets[user.id] = target
                return target
        if self.is_admin(user.id) or self.cfg.allow_private:
            return DMTarget(user.id, "私聊", "test")
        return None

    async def _private_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                            command: str, args: list[str]) -> None:
        msg, user = update.effective_message, update.effective_user
        target = await self._resolve_target(user, context.bot)
        if not target:
            await msg.reply_text("请先在授权群里发送 /test，然后点击「私聊发送订阅」按钮。")
            return
        target.touch()
        preset = PRESETS[command]
        rest, name_filter, slave = self._parse_args(args)

        subs, uris = extract_sources(" ".join(rest))
        if not subs and not uris and msg.reply_to_message:
            reply = msg.reply_to_message
            subs, uris = extract_sources(reply.text or reply.caption or "")
        if not subs and not uris:
            await msg.reply_text(f"请提供订阅链接或节点链接，例如：\n<code>/{preset.command} https://example.com/sub</code>",
                                 parse_mode=ParseMode.HTML)
            return
        if name_filter:
            try:
                re.compile(name_filter)
            except re.error:
                await msg.reply_text("过滤正则无效。")
                return

        if self._busy(target.chat_id):
            await msg.reply_text("目标群已有任务在运行，请等待完成后再试。")
            return

        status = await msg.reply_text("📥 正在解析节点…")
        proxies: list[dict] = []
        errors: list[str] = []
        for uri in uris:
            p = parse_uri(uri)
            if p:
                proxies.append(p)
            else:
                errors.append("有节点链接无法解析")
        for url in subs:
            try:
                got = await fetch_subscription(url)
                if not got:
                    errors.append("订阅中没有找到节点")
                proxies.extend(got)
            except SubscriptionError as e:
                errors.append(str(e))

        nodes, skipped = to_api_nodes(proxies, name_filter, self.cfg.max_nodes)
        if not nodes:
            detail = "；".join(dict.fromkeys(errors)) or "没有符合条件的节点"
            await self._edit(status, f"❌ 没有可测试的节点：{esc(detail)}")
            return

        backends = await self._selectable_backends()
        chosen = None
        if slave:
            chosen = self._match_backend(backends, slave)
            if not chosen:
                await self._edit(status, f"❌ 后端 <code>{esc(slave)}</code> 不存在、离线或不允许使用，发送 /backends 查看可用后端。")
                return
        elif self.cfg.default_slave_id:
            chosen = self._match_backend(backends, self.cfg.default_slave_id)

        sel = Selection(
            owner=user, chat_id=target.chat_id, target_title=target.title, status=status, nodes=nodes,
            skipped=skipped, name_filter=name_filter, slave=chosen and chosen["client_id"],
            slave_name=chosen and chosen.get("display_name"), options=set(preset.options), backends=backends,
        )
        ask_backend = self.cfg.backend_select and not slave and len(backends) > 1
        if command != "test" and not ask_backend:
            # 快捷命令直接按预设开测
            await self._submit(sel, build_plan(preset.title, preset.options), context.application)
            return
        if command != "test":
            # 快捷命令：先选后端，点击后立即开测
            sel.quick, sel.page = command, "backends"
        self._purge_selections()
        sid = secrets.token_hex(4)
        self.selections[sid] = sel
        await self._render_menu(sid, sel)

    # ------------------------------------------------------------ 测试项选择菜单

    def _purge_selections(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self.selections.items() if now - s.created > SELECTION_TTL]:
            self.selections.pop(sid, None)

    async def _media_scripts(self) -> list[tuple[str, str]]:
        """全局流媒体脚本列表 [(id, name)]，缓存 10 分钟。"""
        if self._scripts is None or time.monotonic() - self._scripts[0] > 600:
            try:
                scripts = await self.api.list_scripts()
            except APIError as e:
                log.warning("获取脚本列表失败：%s", e)
                scripts = []
            media = [(str(s.get("id")), s.get("name") or str(s.get("id")))
                     for s in scripts if s.get("type") == "media" and s.get("id")]
            self._scripts = (time.monotonic(), media)
        return self._scripts[1]

    async def _all_backends(self) -> list[dict]:
        """后端列表，缓存 30 秒（排队数会变化）。"""
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
        """用户可以选择的后端：在线、允许 Copilot 调用、在 ALLOWED_BACKENDS 内，按排队数排序。"""
        backends = [b for b in await self._all_backends() if b.get("is_online") and self._backend_allowed(b)]
        return sorted(backends, key=lambda b: (b.get("speed_pending", 0) + b.get("conn_pending", 0),
                                               b.get("display_name") or ""))

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
        pending = b.get("speed_pending", 0) + b.get("conn_pending", 0)
        return f"{b.get('display_name') or b.get('client_id')} · 排队 {pending}"

    @staticmethod
    def _sel_header(sel: "Selection") -> str:
        text = f"节点 {len(sel.nodes)} 个"
        if sel.skipped:
            text += f"（跳过 {sel.skipped} 个）"
        text += f"\n后端：{esc(sel.slave_name or sel.slave or '自动选择')}"
        if sel.name_filter:
            text += f"\n过滤：<code>{esc(sel.name_filter)}</code>"
        return text

    async def _render_menu(self, sid: str, sel: "Selection") -> None:
        chosen = [TEST_OPTIONS[k][0] for k in TEST_OPTIONS if k in sel.options]
        chosen += [name for i, name in sel.script_list if i in sel.scripts]
        text = (f"<b>选择测试项目</b> · {sel.owner.mention_html()}\n{self._sel_header(sel)}\n\n"
                f"已选：{esc('、'.join(chosen)) if chosen else '（未选择）'}")

        def btn(label: str, action: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(label, callback_data=f"sel:{sid}:{action}")

        rows: list[list[InlineKeyboardButton]] = []
        if sel.page == "backends":
            title = PRESETS[sel.quick].title if sel.quick else "测试"
            text = (f"<b>选择{esc(title)}后端</b> · {sel.owner.mention_html()}\n{self._sel_header(sel)}\n\n"
                    + ("点击后端立即开始测试。" if sel.quick else "选择后端后返回菜单。"))
            pages = max(1, -(-len(sel.backends) // BACKENDS_PER_PAGE))
            sel.backend_page = min(sel.backend_page, pages - 1)
            start = sel.backend_page * BACKENDS_PER_PAGE
            mark = lambda on: "✅ " if on else ""  # noqa: E731
            rows.append([btn(mark(sel.slave is None) + "🤖 自动选择（推荐）", "b:auto")])
            for n, b in enumerate(sel.backends[start:start + BACKENDS_PER_PAGE], start):
                rows.append([btn(mark(sel.slave == b.get("client_id")) + "🟢 " + self._backend_label(b)[:40], f"b:{n}")])
            if pages > 1:
                nav = []
                if sel.backend_page > 0:
                    nav.append(btn("◂ 上一页", f"bp:{sel.backend_page - 1}"))
                nav.append(btn(f"{sel.backend_page + 1}/{pages}", "noop"))
                if sel.backend_page < pages - 1:
                    nav.append(btn("下一页 ▸", f"bp:{sel.backend_page + 1}"))
                rows.append(nav)
            rows.append([btn("✖️ 取消", "x")] if sel.quick else [btn("◂ 返回", "main")])
            await self._edit(sel.status, text, InlineKeyboardMarkup(rows))
            return
        if sel.page == "scripts":
            toggles = [btn(("✅ " if i in sel.scripts else "⬜ ") + name[:20], f"s:{n}")
                       for n, (i, name) in enumerate(sel.script_list)]
            rows += [toggles[i:i + 2] for i in range(0, len(toggles), 2)]
            rows.append([btn("全选", "sa"), btn("清空", "sc"), btn("◂ 返回", "main")])
        else:
            rows.append([btn(p.title.replace("测试", ""), f"p:{k}") for k, p in PRESETS.items()])
            toggles = [btn(("✅ " if k in sel.options else "⬜ ") + label, f"t:{k}")
                       for k, (label, _) in TEST_OPTIONS.items()]
            rows += [toggles[i:i + 2] for i in range(0, len(toggles), 2)]
            rows.append([btn(f"🎬 流媒体解锁（已选 {len(sel.scripts)}）▸", "scripts")])
            rows.append([btn(f"🖥 后端：{(sel.slave_name or sel.slave or '自动选择')[:24]} ▸", "backends")])
        rows.append([btn("▶️ 开始测试", "go"), btn("✖️ 取消", "x")])
        await self._edit(sel.status, text, InlineKeyboardMarkup(rows))

    async def _on_select(self, q, context: ContextTypes.DEFAULT_TYPE) -> None:
        _, sid, action = (q.data or "").split(":", 2)
        sel = self.selections.get(sid)
        if not sel or time.monotonic() - sel.created > SELECTION_TTL:
            self.selections.pop(sid, None)
            await q.answer("选择已过期，请重新发送命令。", show_alert=True)
            return
        if q.from_user.id != sel.owner.id and not self.is_admin(q.from_user.id):
            await q.answer("只有发起人可以操作。")
            return

        kind, _, arg = action.partition(":")
        if kind == "t" and arg in TEST_OPTIONS:
            sel.options ^= {arg}
        elif kind == "p" and arg in PRESETS:
            sel.options = set(PRESETS[arg].options)
        elif kind == "scripts":
            sel.script_list = await self._media_scripts()
            if not sel.script_list:
                await q.answer("暂无可用的流媒体脚本。", show_alert=True)
                return
            sel.page = "scripts"
        elif kind == "main":
            sel.page = "main"
        elif kind == "backends":
            sel.backends = await self._selectable_backends()
            sel.page, sel.backend_page = "backends", 0
        elif kind == "bp" and arg.isdigit():
            sel.backend_page = int(arg)
        elif kind == "noop":
            await q.answer()
            return
        elif kind == "b":
            if arg == "auto":
                sel.slave = sel.slave_name = None
            elif arg.isdigit() and int(arg) < len(sel.backends):
                b = sel.backends[int(arg)]
                sel.slave, sel.slave_name = b.get("client_id"), b.get("display_name")
            else:
                await q.answer()
                return
            if sel.quick:
                if self._busy(sel.chat_id):
                    await q.answer("本群已有任务在运行，请等待完成后再试。", show_alert=True)
                    return
                self.selections.pop(sid, None)
                await q.answer("正在提交…")
                preset = PRESETS[sel.quick]
                await self._submit(sel, build_plan(preset.title, preset.options), context.application)
                return
            sel.page = "main"
        elif kind == "s" and arg.isdigit() and int(arg) < len(sel.script_list):
            sel.scripts ^= {sel.script_list[int(arg)][0]}
        elif kind == "sa":
            sel.scripts = {i for i, _ in sel.script_list}
        elif kind == "sc":
            sel.scripts = set()
        elif kind == "x":
            self.selections.pop(sid, None)
            await q.answer()
            await self._edit(sel.status, "已取消。")
            return
        elif kind == "go":
            if not sel.options and not sel.scripts:
                await q.answer("请至少选择一项。", show_alert=True)
                return
            if self._busy(sel.chat_id):
                await q.answer("本群已有任务在运行，请等待完成后再试。", show_alert=True)
                return
            self.selections.pop(sid, None)
            await q.answer("正在提交…")
            title = next((p.title for p in PRESETS.values() if set(p.options) == sel.options and not sel.scripts),
                         "自定义测试")
            await self._submit(sel, build_plan(title, sel.options, tuple(sorted(sel.scripts))), context.application)
            return
        await q.answer()
        await self._render_menu(sid, sel)

    # ------------------------------------------------------------ 提交与跟踪

    async def _submit(self, sel: "Selection", plan: TestPlan, application: Application) -> None:
        user, status = sel.owner, sel.status
        await self._edit(status, f"🚀 正在提交任务…\n{self._sel_header(sel)}")
        task_name = f"TG {plan.title} · {user.full_name}"[:128]
        try:
            data = await self.api.submit_task(task_name, sel.nodes, list(plan.matrices), slave_id=sel.slave)
        except APIError as e:
            await self._edit(status, f"❌ 提交任务失败：{esc(e)}")
            return
        task_id = (data or {}).get("task_id")
        if not task_id:
            await self._edit(status, "❌ 提交任务失败：API 未返回任务 ID")
            return

        self.running.setdefault(sel.chat_id, set()).add(task_id)
        self.owners[task_id] = user.id
        header = f"<b>{esc(plan.title)}</b> · {user.mention_html()}\n任务 <code>{task_id}</code>\n{self._sel_header(sel)}"
        if sel.chat_id != status.chat_id:
            # 私聊提交，进度和结果发到群里并 @ 发起人
            await self._edit(status, f"✅ 已提交，进度和结果将发送到群「{esc(sel.target_title)}」。\n"
                                     f"任务 <code>{task_id}</code>")
            try:
                status = await application.bot.send_message(sel.chat_id, f"{header}\n\n⏳ 排队中",
                                                            parse_mode=ParseMode.HTML)
            except TelegramError as e:
                log.warning("向群 %s 发送任务消息失败：%s", sel.chat_id, e)
                await self._edit(sel.status, f"⚠️ 无法在群里发送消息（{esc(e)}），结果改为发到这里。\n任务 <code>{task_id}</code>")
                status = sel.status
        log.info("chat=%s user=%s 提交任务 %s（%d 节点，%d 矩阵）",
                 sel.chat_id, user.id, task_id, len(sel.nodes), len(plan.matrices))
        application.create_task(self._watch(status, task_id, plan, header, sel.chat_id), name=f"watch-{task_id}")

    async def _edit(self, msg: Message, text: str, markup=None) -> None:
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                disable_web_page_preview=True)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("编辑消息失败：%s", e)
        except TelegramError as e:
            log.warning("编辑消息失败：%s", e)

    async def _watch(self, status: Message, task_id: str, plan: TestPlan, header: str, chat_id: int) -> None:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("取消任务", callback_data=f"cancel:{task_id}")]])
        started = time.monotonic()
        last_text = ""
        task: dict = {}
        try:
            while True:
                try:
                    task = await self.api.get_task(task_id)
                except APIError as e:
                    log.warning("查询任务 %s 失败：%s", task_id, e)
                    task = task or {}
                st = task.get("status", "pending")
                if st in FINAL_STATUSES:
                    break
                if time.monotonic() - started > self.cfg.task_timeout:
                    await self._edit(status, f"{header}\n\n⌛ 等待超时，可稍后使用 /result {task_id} 查看结果。")
                    return

                done, total = task.get("completed_nodes", 0), task.get("node_count", 0)
                if st == "running":
                    try:
                        prog = await self.api.get_progress(task_id)
                        done, total = prog.get("completed_count", done), prog.get("total_count", total)
                    except APIError:
                        pass
                line = f"{STATUS_TEXT.get(st, st)}"
                if task.get("slave_name"):
                    line += f" · 后端 {esc(task['slave_name'])}"
                text = f"{header}\n\n{line}\n<code>{progress_bar(done, total)}</code> {done}/{total}"
                if text != last_text:
                    await self._edit(status, text, keyboard)
                    last_text = text
                await asyncio.sleep(self.cfg.poll_interval)

            st = task.get("status")
            extra = []
            if task.get("duration_ms"):
                extra.append(f"耗时 {task['duration_ms'] / 1000:.0f}s")
            if task.get("credit_cost") is not None:
                extra.append(f"消耗 {task['credit_cost']} 积分")
            summary = f"{header}\n\n{STATUS_TEXT.get(st, st)}" + (f" · {' · '.join(extra)}" if extra else "")
            if st == "failed" and task.get("error_msg"):
                summary += f"\n原因：{esc(task['error_msg'])}"
            await self._edit(status, summary)
            if st == "completed":
                await self._send_result(status, task_id, plan.views, plan.sort, header)
        except Exception:
            log.exception("跟踪任务 %s 出错", task_id)
            await self._edit(status, f"{header}\n\n⚠️ 跟踪任务时出错，可使用 /result {task_id} 查看结果。")
        finally:
            self.running.get(chat_id, set()).discard(task_id)

    async def _send_result(self, reply_to: Message, task_id: str, views: tuple[str, ...],
                           sort: str | None, header: str) -> None:
        entries: list[dict] = []
        try:
            result = await self.api.get_result(task_id)
            entries = ((result or {}).get("result") or {}).get("Results") or []
        except APIError as e:
            log.info("获取任务 %s 结果失败：%s", task_id, e)

        stats = format_stats(entries) if entries else ""
        caption = (f"{header}\n{stats}" if header else f"任务 <code>{task_id}</code>\n{stats}").strip()
        caption = caption[:1000]

        sent = False
        for view in views:
            try:
                image = await self.api.export_image(task_id, view, sort)
            except APIError as e:
                log.info("导出任务 %s 的 %s 失败：%s", task_id, view, e)
                continue
            # 多张图时只在第一张附带说明
            cap = caption if not sent else None
            try:
                await reply_to.reply_photo(io.BytesIO(image), caption=cap, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                # 节点多时图片过长，Telegram 不接受为 photo，改为文件发送
                log.info("以图片发送失败（%s），改为文件发送", e)
                await reply_to.reply_document(io.BytesIO(image), filename=f"{task_id}-{view}.png",
                                              caption=cap, parse_mode=ParseMode.HTML)
            sent = True
        if sent:
            return

        if not entries:
            await reply_to.reply_text(f"{caption}\n\n无法获取结果。", parse_mode=ParseMode.HTML)
            return
        text = f"{caption}\n\n{format_result_text(entries)}"
        # 按行切分，避免截断 HTML 标签
        for chunk in _split(text, 4000):
            await reply_to.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

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


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("test", "自选测试项目"),
        BotCommand("speed", "测速"),
        BotCommand("ping", "延迟测试"),
        BotCommand("udp", "UDP 类型测试"),
        BotCommand("topo", "拓扑分析"),
        BotCommand("backends", "后端列表"),
        BotCommand("tasks", "最近任务"),
        BotCommand("result", "获取任务结果"),
        BotCommand("cancel", "取消任务"),
        BotCommand("help", "帮助"),
    ])


def main() -> None:
    cfg = Config.from_env()
    bot = SpeedBot(cfg)

    async def _post_shutdown(app: Application) -> None:
        await bot.api.close()

    app = (Application.builder().token(cfg.bot_token)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    # 先于其他处理器检查群消息中的订阅链接
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION),
                                   bot.on_group_message), group=-1)
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, bot.on_private_text))
    app.add_handler(CommandHandler("id", bot.cmd_id))
    app.add_handler(CommandHandler(list(PRESETS), bot.cmd_test))
    app.add_handler(CommandHandler("backends", bot.cmd_backends))
    app.add_handler(CommandHandler("tasks", bot.cmd_tasks))
    app.add_handler(CommandHandler("cancel", bot.cmd_cancel))
    app.add_handler(CommandHandler("result", bot.cmd_result))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^(cancel|sel):"))
    log.info("Bot 启动，授权群组：%s", cfg.allowed_chat_ids or "不限")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
