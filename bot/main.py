"""SpeedCentre+ Telegram 群组测速 Bot：在群里测试配置文件中固定的机场订阅。"""
import asyncio
import io
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
    filters,
)

from .api import APIError, SCPClient
from .config import Config
from .formatter import PRESETS, build_plan, esc, format_result_text, format_stats, progress_bar
from .quota import DailyQuota
from .subscription import (
    DEFAULT_SUB_LINK_PATTERN, SubscriptionError, contains_sensitive_link, extract_sources, fetch_subscription,
    parse_uri, to_api_nodes,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("speed_bot")

FINAL_STATUSES = {"completed", "failed", "canceled"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

SELECTION_TTL = 600  # 选择菜单的有效期（秒）
BACKENDS_PER_PAGE = 5
NOTICE_TTL = 60  # 删除订阅链接后的提示消息自动删除时间（秒）
SPEED_PLAN = build_plan(PRESETS["speed"].title, PRESETS["speed"].options)
# 结果图排序方式（按钮文字, export 的 sort 参数；空字符串为订阅原顺序）
SORTS = [
    ("📋 订阅顺序（默认）", ""),
    ("🀄 节点名（升序）", "name_asc"),
    ("🚀 平均速度（升序）", "avg_speed_asc"),
    ("🚀 平均速度（降序）", "avg_speed_desc"),
]


@dataclass
class Selection:
    """/speed 发出后、提交任务前的选择状态（订阅 → 后端 → 排序）。"""
    owner: User
    chat_id: int
    status: Message  # 群里的菜单/进度消息
    name_filter: str | None = None
    slave_arg: str | None = None  # -s 指定的后端，指定后跳过后端选择
    sub: tuple[str, str] | None = None  # (订阅名, 链接)
    nodes: list[dict] = field(default_factory=list)
    skipped: int = 0
    slave: str | None = None  # 选定的后端 ID，None 表示自动选择
    slave_name: str | None = None
    backends: list[dict] = field(default_factory=list)  # 可选后端快照
    backend_page: int = 0
    sort: str | None = None  # None 表示未选择（使用平均速度降序）
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

在群里发送 <code>/speed</code> 测试机场节点当前的速度，依次选择订阅、测试后端和排序方式，
测速进度和结果图会发在群里。

<b>命令</b>：
/speed — 测速（配置了多个订阅时会让你选择）
/speed 订阅名 — 直接测速指定订阅
/sub — 查看可测试的订阅和你今日剩余次数
/backends — 测试后端列表
/result 任务ID — 重新获取结果图

<b>可选参数</b>：
<code>-f 正则</code> 只测名称匹配的节点，例如 <code>/speed -f "香港|HK"</code>
<code>-s 后端ID或名称</code> 直接指定测试后端

{quota}"""


class SpeedBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = SCPClient(cfg.api_key, cfg.api_base)
        self.quota = DailyQuota(os.path.join(cfg.data_dir, "usage.json"), cfg.daily_limit, cfg.timezone)
        self.running: dict[int, set[str]] = {}  # chat_id -> task_ids
        self.owners: dict[str, int] = {}  # task_id -> user_id
        self.selections: dict[str, Selection] = {}  # 菜单 id -> 选择状态
        self._backends: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------ 权限与次数

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.cfg.admin_user_ids

    async def guard(self, update: Update) -> bool:
        """只在授权群组中可用。"""
        chat = update.effective_chat
        if chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text("请在机场群组里使用此 Bot。")
            return False
        if self.cfg.allowed_chat_ids and chat.id not in self.cfg.allowed_chat_ids:
            log.info("拒绝来自 chat=%s 的请求", chat.id)
            await update.effective_message.reply_text(f"本群未授权使用此 Bot。群组 ID：<code>{chat.id}</code>",
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

    # ------------------------------------------------------------ 基础命令

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        quota = f"每人每天可测速 {self.cfg.daily_limit} 次，管理员不限。" if self.cfg.daily_limit > 0 else ""
        if user and update.effective_chat.type != ChatType.PRIVATE:
            quota += self._quota_text(user.id)
        await update.effective_message.reply_text(HELP_TEXT.format(quota=quota), parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            f"群组 ID：<code>{update.effective_chat.id}</code>\n用户 ID：<code>{update.effective_user.id}</code>",
            parse_mode=ParseMode.HTML)

    async def cmd_sub(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        names = "\n".join(f"• <code>{esc(name)}</code>" for name, _ in self.cfg.subscriptions)
        await update.effective_message.reply_text(
            f"<b>可测试的订阅</b>\n{names}\n\n用法：<code>/speed 订阅名</code>\n{self._quota_text(update.effective_user.id)}",
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
        lines = ["<b>后端列表</b>（🟢 在线 · 🔴 离线 · 🚫 不可选）"]
        for b in backends:
            state = "🟢" if b.get("is_online") else "🔴"
            if not self._backend_allowed(b):
                state = "🚫"
            lines.append(
                f"{state} <b>{esc(b.get('display_name') or '-')}</b>\n"
                f"    ID <code>{esc(b.get('client_id'))}</code> · 排队 {b.get('speed_pending', 0)}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        msg = update.effective_message
        texts = [" ".join(context.args or [])]
        if msg.reply_to_message:
            texts.append(msg.reply_to_message.text or msg.reply_to_message.caption or "")
        task_id = next((m.group(0) for m in map(UUID_RE.search, texts) if m), None)
        if not task_id:
            await msg.reply_text("用法：/result 任务ID，或回复任务消息。")
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

    # ------------------------------------------------------------ 群内订阅保护

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
        """授权群内出现节点链接或疑似订阅链接时立即删除，避免泄露。"""
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not msg or not self.cfg.delete_sub_message or not user or user.is_bot:
            return
        if self.cfg.allowed_chat_ids and chat.id not in self.cfg.allowed_chat_ids:
            return
        pattern = self.cfg.sub_link_pattern or DEFAULT_SUB_LINK_PATTERN
        if not contains_sensitive_link(msg.text or msg.caption or "", pattern):
            return
        log.info("删除群 %s 中用户 %s 发送的订阅链接", chat.id, user.id)
        if await self._delete(msg):
            try:
                notice = await context.bot.send_message(
                    chat.id, f"{user.mention_html()} 已删除你发送的订阅/节点链接，避免泄露。"
                             f"本群只测速固定订阅，发送 /sub 查看。", parse_mode=ParseMode.HTML)
                context.application.create_task(self._delete_later(notice, NOTICE_TTL))
            except TelegramError as e:
                log.warning("发送提示失败：%s", e)
        raise ApplicationHandlerStop

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
        if not await self.guard(update):
            return
        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        # 群里不接受任何订阅链接
        if any(extract_sources(msg.text or "")):
            if self.cfg.delete_sub_message:
                await self._delete(msg)
            await context.bot.send_message(chat.id, f"{user.mention_html()} 本群只测速固定订阅，请发送 /sub 查看。",
                                           parse_mode=ParseMode.HTML)
            return
        rest, name_filter, slave = self._parse_args(context.args or [])
        if name_filter:
            try:
                re.compile(name_filter)
            except re.error:
                await msg.reply_text("过滤正则无效。")
                return
        if self._remaining(user.id) == 0:
            await msg.reply_text(self._out_of_quota())
            return
        if self._busy(chat.id):
            await msg.reply_text("本群已有测速任务在运行，请等待完成后再试。")
            return

        sub = None
        if rest:
            sub = self._match_sub(rest[0])
            if not sub:
                names = "、".join(f"<code>{esc(n)}</code>" for n, _ in self.cfg.subscriptions)
                await msg.reply_text(f"未找到订阅「{esc(rest[0])}」。可测试的订阅：{names}", parse_mode=ParseMode.HTML)
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
        if await self._load_nodes(sel):
            await self._next_step(sid, sel, context.application)

    async def _load_nodes(self, sel: Selection) -> bool:
        """拉取订阅、解析节点，并确定可选后端。失败时在状态消息里说明并返回 False。"""
        subs, uris = extract_sources(sel.sub[1])
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
        sel.nodes, sel.skipped = to_api_nodes(proxies, sel.name_filter, self.cfg.max_nodes)
        if not sel.nodes:
            detail = "；".join(dict.fromkeys(errors)) or "没有符合条件的节点"
            await self._edit(sel.status, f"❌ 任务 <b>{esc(sel.label)}</b> 没有可测试的节点：{esc(detail)}")
            return False

        sel.backends = await self._selectable_backends()
        chosen = None
        if sel.slave_arg:
            chosen = self._match_backend(sel.backends, sel.slave_arg)
            if not chosen:
                await self._edit(sel.status, f"❌ 后端 <code>{esc(sel.slave_arg)}</code> 不存在、离线或不允许使用，"
                                             f"发送 /backends 查看可用后端。")
                return False
        elif self.cfg.default_slave_id:
            chosen = self._match_backend(sel.backends, self.cfg.default_slave_id)
        if chosen:
            sel.slave, sel.slave_name = chosen["client_id"], chosen.get("display_name")
        return True

    async def _next_step(self, sid: str, sel: Selection, application: Application) -> None:
        """节点就绪后：按需选后端 → 选排序 → 提交。"""
        if self.cfg.backend_select and not sel.slave_arg and len(sel.backends) > 1 and sel.page != "sort":
            sel.page = "backends"
        elif self.cfg.sort_select and sel.sort is None:
            sel.page = "sort"
        else:
            self.selections.pop(sid, None)
            await self._submit(sel, application)
            return
        self.selections[sid] = sel
        await self._render_menu(sid, sel)

    # ------------------------------------------------------------ 选择菜单

    def _purge_selections(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self.selections.items() if now - s.created > SELECTION_TTL]:
            self.selections.pop(sid, None)

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
        stop = btn("❌ 终止操作", "x")
        who = sel.owner.mention_html()
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
        rows.append([stop])
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
            await q.answer()
            await self._edit(sel.status, f"❌ 任务 <b>{esc(sel.label)}</b> 已终止。")
            return
        if kind == "u" and sel.page == "subs" and 0 <= idx < len(self.cfg.subscriptions):
            await q.answer()
            sel.sub = self.cfg.subscriptions[idx]
            sel.page = "loading"
            await self._edit(sel.status, f"📥 任务 <b>{esc(sel.label)}</b> 正在解析节点…")
            if not await self._load_nodes(sel):
                self.selections.pop(sid, None)
                return
            await self._next_step(sid, sel, context.application)
            return
        if kind == "bp" and sel.page == "backends" and idx >= 0:
            sel.backend_page = idx
            await q.answer()
            await self._render_menu(sid, sel)
            return
        if kind == "b" and sel.page == "backends" and (arg == "auto" or 0 <= idx < len(sel.backends)):
            if arg == "auto":
                sel.slave = sel.slave_name = None
            else:
                sel.slave, sel.slave_name = sel.backends[idx].get("client_id"), sel.backends[idx].get("display_name")
            sel.page = "sort"
        elif kind == "o" and sel.page == "sort" and 0 <= idx < len(SORTS):
            sel.sort = SORTS[idx][1]
        else:
            await q.answer()
            return
        if self._busy(sel.chat_id):
            await q.answer("本群已有测速任务在运行，请等待完成后再试。", show_alert=True)
            return
        await q.answer()
        await self._next_step(sid, sel, context.application)

    # ------------------------------------------------------------ 提交与跟踪

    async def _submit(self, sel: Selection, application: Application) -> None:
        user, status = sel.owner, sel.status
        if self._remaining(user.id) == 0:
            await self._edit(status, self._out_of_quota())
            return
        if self._busy(sel.chat_id):
            await self._edit(status, "本群已有测速任务在运行，请等待完成后再试。")
            return
        await self._edit(status, f"🚀 任务 <b>{esc(sel.label)}</b> 正在提交…")
        task_name = f"{sel.label} · 测速 · {user.full_name}"[:128]
        try:
            data = await self.api.submit_task(task_name, sel.nodes, list(SPEED_PLAN.matrices), slave_id=sel.slave)
        except APIError as e:
            await self._edit(status, f"❌ 任务 <b>{esc(sel.label)}</b> 提交失败：{esc(e)}")
            return
        task_id = (data or {}).get("task_id")
        if not task_id:
            await self._edit(status, "❌ 提交任务失败：API 未返回任务 ID")
            return

        remaining = None if self.is_admin(user.id) else self.quota.consume(user.id)
        self.running.setdefault(sel.chat_id, set()).add(task_id)
        self.owners[task_id] = user.id
        info = f"发起人 {user.mention_html()} · 节点 {len(sel.nodes)} 个"
        if sel.name_filter:
            info += f" · 过滤 <code>{esc(sel.name_filter)}</code>"
        if remaining is not None:
            info += f" · 今日剩余 {remaining} 次"
        info += f"\nID <code>{task_id}</code>"
        sort = "avg_speed_desc" if sel.sort is None else (sel.sort or None)
        view = TaskView(task_id, sel.label, info, sel.slave_name or sel.slave or "自动选择", sort)
        log.info("chat=%s user=%s 提交测速 %s（订阅 %s，%d 节点）", sel.chat_id, user.id, task_id, sel.label, len(sel.nodes))
        application.create_task(self._watch(status, view, sel.chat_id), name=f"watch-{task_id}")

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
                    url = self.cfg.share_url.replace("{uuid}", share["uuid"])
            except APIError as e:
                log.info("创建任务 %s 的分享失败：%s", v.task_id, e)
        url = url or self._task_url(v.task_id)
        return InlineKeyboardMarkup([[InlineKeyboardButton("📊 查看详情", url=url)]]) if url else None

    async def _watch(self, status: Message, v: TaskView, chat_id: int) -> None:
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
                    await self._edit(status, self._task_text(
                        v, st, extra=f"⌛ 等待超时，可稍后使用 /result {v.task_id} 查看结果。"))
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
                    await self._edit(status, text, keyboard)
                    last_text = text
                await asyncio.sleep(self.cfg.poll_interval)

            st = task.get("status")
            extra = []
            if task.get("duration_ms"):
                extra.append(f"耗时 {task['duration_ms'] / 1000:.0f}s")
            if st == "failed" and task.get("error_msg"):
                extra.append(f"原因：{esc(task['error_msg'])}")
            summary = self._task_text(v, st, backend=task.get("slave_name"), extra=" · ".join(extra))
            await self._edit(status, summary)
            if st == "completed":
                await self._send_result(status, v.task_id, v.sort, summary, await self._share_markup(v))
        except Exception:
            log.exception("跟踪任务 %s 出错", v.task_id)
            await self._edit(status, self._task_text(
                v, "unknown", extra=f"⚠️ 跟踪任务时出错，可使用 /result {v.task_id} 查看结果。"))
        finally:
            self.running.get(chat_id, set()).discard(v.task_id)

    async def _send_result(self, reply_to: Message, task_id: str, sort: str | None, header: str,
                           markup: InlineKeyboardMarkup | None = None) -> None:
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
        if image:
            try:
                await reply_to.reply_photo(io.BytesIO(image), caption=caption, parse_mode=ParseMode.HTML,
                                           reply_markup=markup)
            except BadRequest as e:
                # 节点多时图片过长，Telegram 不接受为 photo，改为文件发送
                log.info("以图片发送失败（%s），改为文件发送", e)
                await reply_to.reply_document(io.BytesIO(image), filename=f"{task_id}.png", caption=caption,
                                              parse_mode=ParseMode.HTML, reply_markup=markup)
            return

        if not entries:
            await reply_to.reply_text(f"{caption}\n\n无法获取结果。", parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        # 按行切分，避免截断 HTML 标签
        chunks = _split(f"{caption}\n\n{format_result_text(entries)}", 4000)
        for i, chunk in enumerate(chunks):
            await reply_to.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                      reply_markup=markup if i == len(chunks) - 1 else None)


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
        BotCommand("speed", "测速机场节点"),
        BotCommand("sub", "可测试的订阅与剩余次数"),
        BotCommand("backends", "测试后端列表"),
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
    app.add_handler(CommandHandler(["start", "help"], bot.cmd_help))
    app.add_handler(CommandHandler("id", bot.cmd_id))
    app.add_handler(CommandHandler("speed", bot.cmd_speed))
    app.add_handler(CommandHandler("sub", bot.cmd_sub))
    app.add_handler(CommandHandler("backends", bot.cmd_backends))
    app.add_handler(CommandHandler("result", bot.cmd_result))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^(cancel|sel):"))
    log.info("Bot 启动，订阅：%s，授权群组：%s，每日次数：%s",
             "、".join(n for n, _ in cfg.subscriptions), cfg.allowed_chat_ids or "不限",
             cfg.daily_limit if cfg.daily_limit > 0 else "不限")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
