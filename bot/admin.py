"""管理员命令：授权群、管理员、封禁、本机场订阅、自动测速时间表、各项限制，以及任务管理和运行状态。

设置保存在 DATA_DIR/settings.json，立即生效、重启不丢，并覆盖 .env 中的同名配置。
管理命令建议在私聊里使用；在群里使用时，命令和回复同样会被自动删除。
"""
import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .formatter import esc, fmt_duration
from .schedule import ScheduleError, parse_schedule
from .subscription import extract_sources

log = logging.getLogger("speed_bot")

# 所有人可用的命令（同时是默认的命令菜单）；/start、/id 另外注册
USER_COMMANDS = [
    ("speed", "测速（私聊发送订阅）"),
    ("sub", "本机场订阅与剩余次数"),
    ("backends", "测试后端列表"),
    ("cancel", "取消测速任务（回复进度消息）"),
    ("result", "重新获取结果图"),
    ("help", "帮助"),
]
# 管理员命令，只出现在管理员私聊的命令菜单里
ADMIN_COMMANDS = [
    ("autotest", "立即测速本机场订阅"),
    ("settings", "查看全部设置"),
    ("schedule", "自动测速时间（时间点 / 间隔 / cron）"),
    ("airport", "本机场订阅：add 名称 链接 / del 名称"),
    ("group", "授权群：add / del 群组ID"),
    ("admin", "管理员：add / del 用户ID（超级管理员）"),
    ("ban", "禁止用户测速"),
    ("unban", "解除禁止"),
    ("cooldown", "群成员两次测速的间隔"),
    ("maxnodes", "群成员单次最多测几个节点"),
    ("limit", "群成员每天测速次数"),
    ("creditalert", "每日积分消耗提醒"),
    ("alert", "本机场异常节点提醒"),
    ("pin", "置顶自动测速结果 on / off"),
    ("status", "运行状态"),
    ("tasks", "进行中的任务"),
    ("stopall", "终止所有测速任务"),
]
MAX_SUB_NAME = 32
MAX_LISTED_TASKS = 20

SCHEDULE_HELP = """<b>用法</b>
<code>/schedule 09:00,21:00</code> 每天固定时间
<code>/schedule 6h</code> 每 6 小时（0、6、12、18 点整）
<code>/schedule 30m</code> 每 30 分钟
<code>/schedule 0 8-22/2 * * *</code> cron：8 点到 22 点每 2 小时
<code>/schedule 30 9 * * 1-5</code> cron：工作日 09:30
<code>/schedule off</code> 关闭自动测速
cron 依次为「分 时 日 月 周」，支持 * , - / 和 mon、jan 等缩写；时间按 {tz} 计算，两次至少间隔 10 分钟。"""

_DURATION_UNITS = {"": 1, "s": 1, "秒": 1, "m": 60, "min": 60, "分": 60, "分钟": 60,
                   "h": 3600, "小时": 3600, "d": 86400, "天": 86400}


def parse_duration(text: str) -> int:
    """“30”“30s”“5m”“1h”“1d”（off 表示 0）转换成秒数。"""
    text = text.strip().lower()
    if text in ("off", "none", "false"):
        return 0
    m = re.fullmatch(r"(\d{1,7})\s*(s|秒|m|min|分钟|分|h|小时|d|天)?", text)
    if not m:
        raise ValueError(text)
    return int(m.group(1)) * _DURATION_UNITS[m.group(2) or ""]


def parse_number(text: str) -> int:
    text = text.strip().lower().rstrip("%")
    if text in ("off", "none", "false"):
        return 0
    if not text.isdigit():
        raise ValueError(text)
    return int(text)


def _user_id(text: str) -> int | None:
    return int(text) if re.fullmatch(r"\d{1,20}", text) else None


def _chat_id(text: str) -> int | None:
    return int(text) if re.fullmatch(r"-\d{1,20}", text) else None


class AdminCommands:
    """SpeedBot 的管理员命令部分（作为基类混入，使用 SpeedBot 的属性和方法）。"""

    # 可以直接用一个数字修改的设置：命令 -> (Config 字段, 名称, 解析, 显示, 最大值, 用法)
    NUMBER_SETTINGS = {
        "cooldown": ("cooldown_seconds", "群成员测速冷却", parse_duration,
                     lambda self, v: fmt_duration(v) if v else "不限", 7 * 86400,
                     "/cooldown 5m（支持 30s、5m、1h，0 表示不限）"),
        "maxnodes": ("member_max_nodes", "群成员单次节点上限", parse_number,
                     lambda self, v: f"{v} 个" if v else f"不限（受 MAX_NODES={self.cfg.max_nodes} 限制）", 100000,
                     "/maxnodes 50（0 表示不限）"),
        "limit": ("daily_limit", "群成员每日测速次数", parse_number,
                  lambda self, v: f"{v} 次" if v else "不限", 100000, "/limit 3（0 表示不限）"),
        "creditalert": ("credit_alert", "每日积分提醒", parse_number,
                        lambda self, v: f"当天消耗超过 {v} 积分时私聊提醒管理员" if v else "不提醒", 10 ** 12,
                        "/creditalert 5000（0 表示不提醒）"),
        "alert": ("anomaly_percent", "本机场异常提醒", parse_number,
                  lambda self, v: f"自动测速失败或 ≥{v}% 节点没有速度时私聊提醒管理员" if v else "不提醒", 100,
                  "/alert 50（0 表示不提醒）"),
    }

    # ------------------------------------------------------------ 公用

    async def _admin_guard(self, update: Update, super_only: bool = False) -> bool:
        msg, user = update.effective_message, update.effective_user
        self.expire(msg)  # 群里的管理命令同样按时删除
        if self.is_super(user.id) or (self.is_admin(user.id) and not super_only):
            return True
        await self._say(msg, "只有超级管理员（.env 中的 ADMIN_USER_IDS）可以使用这个命令。"
                        if self.is_admin(user.id) else "只有管理员可以使用这个命令。")
        return False

    def _save(self, key: str, value) -> str:
        """修改并保存一项设置；保存失败时设置仍然生效，返回附加到回复里的提示。"""
        setattr(self.cfg, key, value)
        try:
            self.settings.set(key, value)
        except OSError as e:
            log.warning("保存设置 %s 失败：%s", key, e)
            return f"\n⚠️ 已生效，但保存到文件失败（{esc(e)}），重启后会恢复原设置。"
        return ""

    async def _reply(self, update: Update, text: str, **kw) -> Message:
        kw.setdefault("parse_mode", ParseMode.HTML)
        kw.setdefault("disable_web_page_preview", True)
        return await self._say(update.effective_message, text, **kw)

    @staticmethod
    def _target_user(msg: Message, args: list[str]) -> int | None:
        """命令里的用户 ID，或被回复的消息的发送者。"""
        if args:
            return _user_id(args[0])
        reply = msg.reply_to_message
        if reply and reply.from_user and not reply.from_user.is_bot:
            return reply.from_user.id
        return None

    def admin_help(self) -> str:
        lines = ["<b>管理员命令</b>（建议私聊使用；在群里使用时命令和回复也会自动删除）"]
        lines += [f"/{name} — {esc(desc)}" for name, desc in ADMIN_COMMANDS]
        return "\n".join(lines)

    async def set_commands(self, bot) -> None:
        """设置命令菜单：所有人看到普通命令，管理员私聊里额外看到管理命令。"""
        await bot.set_my_commands([BotCommand(n, d) for n, d in USER_COMMANDS])
        for uid in sorted(self.admin_ids()):
            await self._set_admin_menu(bot, uid, True)

    async def _set_admin_menu(self, bot, user_id: int, admin: bool) -> None:
        scope = BotCommandScopeChat(user_id)
        try:
            if admin:
                await bot.set_my_commands([BotCommand(n, d) for n, d in USER_COMMANDS + ADMIN_COMMANDS], scope=scope)
            else:
                await bot.delete_my_commands(scope=scope)
        except TelegramError as e:  # 对方还没私聊过 bot
            log.info("设置用户 %s 的命令菜单失败：%s", user_id, e)

    def _schedule_summary(self) -> str:
        if not self.schedule:
            return "未启用"
        text = self.schedule.describe()
        nxt = self.next_run()
        if nxt:
            text += f"，下次 {nxt:%m-%d %H:%M}"
        if not self.cfg.subscriptions:
            text += "（还没有本机场订阅，不会运行）"
        elif not self._auto_chats():
            text += "（没有授权群，不会运行）"
        return text

    # ------------------------------------------------------------ 设置总览

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        cfg = self.cfg
        groups = "、".join(f"<code>{g}</code>" for g in sorted(cfg.allowed_chat_ids)) or "⚠️ 未设置（任何群都能用）"
        subs = "、".join(f"<code>{esc(n)}</code>" for n, _ in cfg.subscriptions) or "无"
        lines = [
            "<b>当前设置</b>（修改立即生效，重启不丢）",
            f"授权群：{groups} → /group",
            f"管理员：{len(self.admin_ids())} 人 → /admin",
            f"禁止测速：{len(cfg.banned_user_ids)} 人 → /ban",
            f"本机场订阅：{subs} → /airport",
            f"自动测速：{esc(self._schedule_summary())} → /schedule",
            f"置顶自动测速结果：{'开' if cfg.pin_auto_result else '关'} → /pin",
        ]
        for name, (key, label, _, fmt, _, _) in self.NUMBER_SETTINGS.items():
            lines.append(f"{label}：{esc(fmt(self, getattr(cfg, key)))} → /{name}")
        await self._reply(update, "\n".join(lines))

    # ------------------------------------------------------------ 授权群、管理员、封禁

    async def cmd_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        chat, args = update.effective_chat, context.args or []
        action = args[0].lower() if args else ""
        groups = set(self.cfg.allowed_chat_ids)
        usage = ("<code>/group add 群组ID</code> 添加授权群（在群里发 /id 获取 ID；也可以直接在要授权的群里发 "
                 "<code>/group add</code>）\n<code>/group del 群组ID</code> 移除")
        if action not in ("add", "del"):
            lines = [f"<b>授权群</b>（{len(groups)} 个）"]
            for gid in sorted(groups):
                lines.append(f"• {esc(await self._chat_title(context.bot, gid))} <code>{gid}</code>")
            if not groups:
                lines.append("⚠️ 未设置：任何群都能使用 bot，并消耗你的积分。")
            await self._reply(update, "\n".join(lines + ["", usage]))
            return
        if len(args) > 1:
            target = _chat_id(args[1])
        else:
            target = chat.id if chat.type != ChatType.PRIVATE else None
        if target is None:
            await self._reply(update, "请提供群组 ID（负数）。\n" + usage)
            return
        note = ""
        if action == "add":
            if target in groups:
                await self._reply(update, f"<code>{target}</code> 已经是授权群。")
                return
            if not groups:
                note = "\n之前未设置授权群（任何群都能用），现在只有授权群可以使用 bot。"
            groups.add(target)
        else:
            if target not in groups:
                await self._reply(update, f"<code>{target}</code> 不是授权群。")
                return
            if len(groups) == 1:
                await self._reply(update, "这是最后一个授权群，不能移除：授权群为空时任何群都能使用 bot。")
                return
            groups.discard(target)
        saved = self._save("allowed_chat_ids", groups)
        self.schedule_changed.set()  # 自动测速发往的群可能变了
        title = await self._chat_title(context.bot, target)
        verb = "添加" if action == "add" else "移除"
        await self._reply(update, f"✅ 已{verb}授权群「{esc(title)}」<code>{target}</code>。{note}{saved}")

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update, super_only=True):
            return
        msg, args = update.effective_message, context.args or []
        action = args[0].lower() if args else ""
        usage = ("<code>/admin add 用户ID</code> 添加管理员（也可以在群里回复某人的消息发送 <code>/admin add</code>）\n"
                 "<code>/admin del 用户ID</code> 移除\n用户可以私聊 bot 发送 /id 查看自己的 ID。")
        if action not in ("add", "del"):
            lines = ["<b>管理员</b>"]
            lines += [f"• <code>{uid}</code> ⭐ 超级管理员（.env）" for uid in sorted(self.cfg.admin_user_ids)]
            lines += [f"• <code>{uid}</code>" for uid in sorted(self.cfg.extra_admin_ids - self.cfg.admin_user_ids)]
            await self._reply(update, "\n".join(lines + ["", usage]))
            return
        uid = self._target_user(msg, args[1:])
        if uid is None:
            await self._reply(update, "请提供用户 ID。\n" + usage)
            return
        extra = set(self.cfg.extra_admin_ids)
        if action == "add":
            if self.is_admin(uid):
                await self._reply(update, f"<code>{uid}</code> 已经是管理员。")
                return
            extra.add(uid)
        else:
            if self.is_super(uid):
                await self._reply(update, "超级管理员在 .env 的 ADMIN_USER_IDS 中设置，不能用命令移除。")
                return
            if uid not in extra:
                await self._reply(update, f"<code>{uid}</code> 不是管理员。")
                return
            extra.discard(uid)
        saved = self._save("extra_admin_ids", extra)
        await self._set_admin_menu(context.bot, uid, action == "add")
        verb = "添加" if action == "add" else "移除"
        await self._reply(update, f"✅ 已{verb}管理员 <code>{uid}</code>。{saved}")

    async def cmd_ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._ban(update, context, True)

    async def cmd_unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._ban(update, context, False)

    async def _ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ban: bool) -> None:
        if not await self._admin_guard(update):
            return
        msg = update.effective_message
        banned = set(self.cfg.banned_user_ids)
        uid = self._target_user(msg, context.args or [])
        if uid is None:
            usage = (f"<code>/{'ban' if ban else 'unban'} 用户ID</code>，或在群里回复某人的消息发送 "
                     f"<code>/{'ban' if ban else 'unban'}</code>")
            lines = [f"<b>禁止测速的用户</b>（{len(banned)} 人）"] + [f"• <code>{u}</code>" for u in sorted(banned)]
            await self._reply(update, "\n".join(lines + ["", usage]))
            return
        if ban:
            if self.is_admin(uid):
                await self._reply(update, "不能禁止管理员。")
                return
            if uid in banned:
                await self._reply(update, f"<code>{uid}</code> 已经被禁止测速。")
                return
            banned.add(uid)
            self.dm_targets.pop(uid, None)
        else:
            if uid not in banned:
                await self._reply(update, f"<code>{uid}</code> 没有被禁止。")
                return
            banned.discard(uid)
        saved = self._save("banned_user_ids", banned)
        await self._reply(update, f"✅ 已{'禁止' if ban else '恢复'}用户 <code>{uid}</code> 测速。{saved}")

    # ------------------------------------------------------------ 本机场订阅与时间表

    async def cmd_airport(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        msg, chat, args = update.effective_message, update.effective_chat, context.args or []
        action = args[0].lower() if args else ""
        private = chat.type == ChatType.PRIVATE
        subs = list(self.cfg.subscriptions)
        usage = ("<code>/airport add 名称 订阅链接</code> 添加（请在私聊里发，避免订阅泄露）\n"
                 "<code>/airport del 名称</code> 删除\n用命令修改后以 bot 保存的列表为准，subscriptions.yaml 不再生效。")
        if action == "add":
            if not private:
                await self._delete(msg)  # 命令里有订阅地址
                await self._reply(update, "请在私聊里添加订阅，避免订阅地址泄露。")
                return
            if len(args) < 3:
                await self._reply(update, usage)
                return
            name = args[1]
            found_subs, uris = extract_sources(" ".join(args[2:]))
            if len(name) > MAX_SUB_NAME:
                await self._reply(update, f"名称最多 {MAX_SUB_NAME} 个字符。")
                return
            if self._match_sub(name):
                await self._reply(update, f"已经有名为「{esc(name)}」的订阅，先用 /airport del 删除。")
                return
            if not found_subs and not uris:
                await self._reply(update, "没有找到订阅链接或节点链接。\n" + usage)
                return
            subs.append((name, " ".join(found_subs + uris)))
            done = f"✅ 已添加本机场订阅「{esc(name)}」，可以发送 /autotest 立即测速检查。"
        elif action == "del":
            sub = self._match_sub(args[1]) if len(args) > 1 else None
            if not sub:
                await self._reply(update, f"没有找到这个订阅。\n{usage}")
                return
            subs.remove(sub)
            done = f"✅ 已删除本机场订阅「{esc(sub[0])}」。"
        else:
            lines = [f"<b>本机场订阅</b>（{len(subs)} 个）"]
            for name, url in subs:
                host = urlsplit(url.split()[0]).hostname if private else None  # 群里只显示名称
                lines.append(f"• <code>{esc(name)}</code>" + (f" · {esc(host)}" if host else ""))
            await self._reply(update, "\n".join(lines + ["", usage]))
            return
        saved = self._save("subscriptions", subs)
        self.schedule_changed.set()
        await self._reply(update, done + saved)

    def _next_runs(self, count: int = 3) -> list[datetime]:
        runs, now = [], datetime.now(self.tz)
        while self.schedule and len(runs) < count:
            now = self.schedule.next_after(now, self.tz)
            if now is None:
                break
            runs.append(now)
        return runs

    async def cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        text = " ".join(context.args or []).strip()
        help_text = SCHEDULE_HELP.format(tz=esc(self.cfg.timezone))
        if not text:
            lines = [f"<b>自动测速</b>：{esc(self._schedule_summary())}"]
            runs = self._next_runs()
            if runs:
                lines.append("接下来：" + "、".join(f"{t:%m-%d %H:%M}" for t in runs))
            await self._reply(update, "\n".join(lines + ["", help_text]))
            return
        try:
            schedule = parse_schedule(text, self.tz)
        except ScheduleError as e:
            await self._reply(update, f"❌ {esc(e)}\n\n{help_text}")
            return
        self.schedule = schedule
        saved = self._save("schedule_spec", schedule.spec if schedule else "")
        self.schedule_changed.set()  # 定时任务立即按新时间表重新计时
        if schedule is None:
            await self._reply(update, "✅ 已关闭自动测速。" + saved)
            return
        runs = "、".join(f"{t:%m-%d %H:%M}" for t in self._next_runs())
        await self._reply(update, f"✅ 自动测速时间已设为：{esc(self._schedule_summary())}\n接下来：{runs}{saved}")

    # ------------------------------------------------------------ 数值设置

    async def _number_setting(self, update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
        if not await self._admin_guard(update):
            return
        key, label, parse, fmt, maximum, usage = self.NUMBER_SETTINGS[command]
        arg = " ".join(context.args or []).strip()
        if not arg:
            await self._reply(update, f"{label}：{esc(fmt(self, getattr(self.cfg, key)))}\n用法：{esc(usage)}")
            return
        try:
            value = parse(arg)
        except ValueError:
            await self._reply(update, f"❌ 格式错误。用法：{esc(usage)}")
            return
        if value > maximum:
            await self._reply(update, f"❌ 最大不能超过 {maximum}。")
            return
        saved = self._save(key, value)
        if key == "daily_limit":
            self.quota.limit = value
        await self._reply(update, f"✅ {label}：{esc(fmt(self, value))}{saved}")

    async def cmd_cooldown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._number_setting(update, context, "cooldown")

    async def cmd_maxnodes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._number_setting(update, context, "maxnodes")

    async def cmd_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._number_setting(update, context, "limit")

    async def cmd_creditalert(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._number_setting(update, context, "creditalert")

    async def cmd_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._number_setting(update, context, "alert")

    async def cmd_pin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        arg = (context.args or [""])[0].lower()
        if arg not in ("on", "off"):
            state = "开" if self.cfg.pin_auto_result else "关"
            await self._reply(update, f"置顶自动测速结果：{state}\n用法：<code>/pin on</code> 或 <code>/pin off</code>"
                                      "（需要 bot 有置顶消息权限）")
            return
        saved = self._save("pin_auto_result", arg == "on")
        await self._reply(update, f"✅ 已{'开启' if arg == 'on' else '关闭'}置顶自动测速结果。{saved}")

    # ------------------------------------------------------------ 任务管理与状态

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """取消任务：回复进度消息、带任务 ID，或者自己只有一个进行中的任务时直接发送。"""
        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if chat.type != ChatType.PRIVATE and not await self.guard(update):
            return
        task_id = self._task_id_from(msg, context.args)
        if not task_id:
            mine = [t for t in self.active.values() if t.owner_id == user.id]
            if len(mine) != 1:
                hint = " 管理员可以发送 /tasks 查看所有任务。" if self.is_admin(user.id) else ""
                await self._say(msg, "用法：回复任务的进度消息发送 /cancel，或发送 /cancel 任务ID。" + hint)
                return
            task_id = mine[0].task_id
        await self._say(msg, await self._cancel(task_id, user.id))

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        auto = "本机场测速进行中（/stopall 可终止剩下的订阅）。" if self._auto_lock.locked() else ""
        if not self.active:
            await self._reply(update, "当前没有进行中的任务。" + auto)
            return
        now = time.monotonic()
        tasks = sorted(self.active.values(), key=lambda t: t.started)
        lines, rows = [f"<b>进行中的任务</b>（{len(tasks)} 个）"], []
        for i, t in enumerate(tasks[:MAX_LISTED_TASKS], 1):
            progress = f"{t.done}/{t.total}" if t.total else ("排队中" if t.status == "pending" else esc(t.status))
            lines.append(f"{i}. <b>{esc(t.label)}</b> · {t.requester} · {progress} · 已 {fmt_duration(now - t.started)}\n"
                         f"    ID <code>{t.task_id}</code>")
            rows.append([InlineKeyboardButton(f"❌ 取消 {i}. {t.label[:24]}", callback_data=f"cancel:{t.task_id}")])
        if len(tasks) > MAX_LISTED_TASKS:
            lines.append(f"… 还有 {len(tasks) - MAX_LISTED_TASKS} 个")
        if auto:
            lines.append(auto)
        await self._reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_stopall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        auto = self._auto_lock.locked()
        if auto:
            self._auto_stop = True
        ids = list(self.active)
        if not ids and not auto:
            await self._reply(update, "当前没有进行中的任务。")
            return
        results = await asyncio.gather(*(self.api.cancel_task(t) for t in ids), return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        lines = [f"已对 {len(ids) - len(errors)} 个任务发起取消。"]
        if errors:
            lines.append(f"{len(errors)} 个取消失败：{esc(errors[0])}")
        if auto:
            lines.append("本轮本机场测速剩下的订阅不再继续。")
        log.info("管理员 %s 终止了所有任务（%d 个）", update.effective_user.id, len(ids))
        await self._reply(update, "\n".join(lines))

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        tests, credits = self.stats.today()
        backends = await self._all_backends()
        online = sum(1 for b in backends if b.get("is_online"))
        alert = f"（提醒阈值 {self.cfg.credit_alert}）" if self.cfg.credit_alert else ""
        lines = [
            "<b>运行状态</b>",
            f"已运行：{fmt_duration(time.monotonic() - self.started)}",
            f"进行中的任务：{len(self.active)} 个" + ("，本机场测速进行中" if self._auto_lock.locked() else ""),
            f"今日测速：{tests} 次，消耗 {credits} 积分{alert}",
            f"自动测速：{esc(self._schedule_summary())}",
            f"授权群 {len(self.cfg.allowed_chat_ids)} 个 · 管理员 {len(self.admin_ids())} 人 · "
            f"禁止测速 {len(self.cfg.banned_user_ids)} 人",
            f"测试后端：在线 {online}/{len(backends)}",
        ]
        await self._reply(update, "\n".join(lines))
