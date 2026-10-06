import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.ext import ApplicationHandlerStop

from bot.config import Config, load_subscriptions, parse_times
from bot.formatter import build_plan
from bot.main import SpeedBot, TaskView
from bot.quota import DailyQuota

SUBS = [("3399", "trojan://pw@hk.com:443#HK"), ("IPLC", "trojan://pw@jp.com:443#JP")]
TASK_ID = "11111111-1111-1111-1111-111111111111"
ADMIN = 9


def test_build_plan():
    plan = build_plan("x", {"rtt", "speed", "geo"}, ("netflix",))
    types = [m["Type"] for m in plan.matrices]
    assert types == ["TEST_PING_RTT", "SPEED_AVERAGE", "SPEED_MAX", "SPEED_PER_SECOND",
                     "GEOIP_INBOUND", "GEOIP_OUTBOUND", "TEST_SCRIPT"]
    assert plan.views == ("normalview", "topologyview") and plan.sort == "avg_speed_desc"


class FakeUser:
    def __init__(self, uid):
        self.id, self.full_name, self.is_bot = uid, f"u{uid}", False

    def mention_html(self):
        return f"<a>u{self.id}</a>"


class FakeMessage:
    def __init__(self, chat_id=-100, text=""):
        self.chat_id, self.text, self.caption = chat_id, text, None
        self.edits, self.replies, self.photos, self.photo_markups = [], [], [], []
        self.deleted = False
        self.reply_to_message = None

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw.get("reply_markup")))

    async def reply_text(self, text, **kw):
        m = FakeMessage(self.chat_id)
        self.replies.append((text, kw.get("reply_markup"), m))
        return m

    async def reply_photo(self, photo, caption=None, **kw):
        self.photos.append(caption)
        self.photo_markups.append(kw.get("reply_markup"))

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, uid):
        self.data, self.from_user, self.answers = data, FakeUser(uid), []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class FakeAPI:
    def __init__(self):
        self.submitted, self.calls = None, []
        self.slave_id = self.sort = self.shared = None

    async def list_backends(self):
        return [
            {"client_id": "SHCT", "display_name": "上海电信@2Gbps", "is_online": True, "allow_public_access": True},
            {"client_id": "DGCT", "display_name": "东莞电信@1Gbps", "is_online": True},
            {"client_id": "US", "display_name": "美国", "is_online": False},
            {"client_id": "PRIV", "display_name": "私有", "is_online": True, "allow_public_access": False},
        ]

    async def submit_task(self, name, nodes, matrices, slave_id=None):
        self.submitted, self.slave_id = (name, nodes, matrices), slave_id
        self.calls.append(name)
        return {"task_id": TASK_ID, "status": "pending"}

    async def get_task(self, task_id):
        return {"status": "completed", "slave_name": "上海电信@2Gbps", "duration_ms": 42000}

    async def get_result(self, task_id):
        return {"result": {"Results": []}}

    async def export_image(self, task_id, view, sort=None):
        self.sort = sort
        return b"img"

    async def create_share(self, task_id, title, hide_private_info=True):
        self.shared = (task_id, title, hide_private_info)
        return {"uuid": "share-1"}


class FakeBot:
    username = "speedbot"

    def __init__(self, members=(1, 2)):
        self.sent, self.members = [], set(members)

    async def send_message(self, chat_id, text, **kw):
        m = FakeMessage(chat_id)
        self.sent.append((chat_id, text, kw.get("reply_markup"), m))
        return m

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member" if user_id in self.members else "left")

    async def get_chat(self, chat_id):
        return SimpleNamespace(title="测速群")


class FakeApp:
    def __init__(self):
        self.tasks = []
        self.bot = FakeBot()

    def create_task(self, coro, name=None):
        self.tasks.append(coro)


class FakeContext:
    def __init__(self, args=None, app=None):
        self.application = app or FakeApp()
        self.bot = self.application.bot
        self.args = args or []

    async def run_tasks(self):
        while self.application.tasks:
            t = self.application.tasks.pop(0)
            if t.__qualname__.endswith("_delete_later"):  # 引导消息的定时删除，不需要等待
                t.close()
            else:
                await t


def make_bot(tmp_path, **kw):
    kw.setdefault("subscriptions", SUBS)
    kw.setdefault("allowed_chat_ids", {-100})
    kw.setdefault("admin_user_ids", {ADMIN})
    bot = SpeedBot(Config(bot_token="t", api_key="k", data_dir=str(tmp_path), **kw))
    bot.api = FakeAPI()
    return bot


def update(text, user_id=1, chat_id=-100, chat_type="supergroup"):
    msg = FakeMessage(chat_id, text)
    upd = SimpleNamespace(effective_message=msg, effective_user=FakeUser(user_id),
                          effective_chat=SimpleNamespace(id=chat_id, type=chat_type))
    return upd, msg


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


async def speed(bot, text="/speed", user_id=ADMIN, ctx=None):
    upd, msg = update(text, user_id)
    ctx = ctx or FakeContext(text.split()[1:])
    await bot.cmd_speed(upd, ctx)
    return msg, ctx


async def click(bot, data, user_id=ADMIN, ctx=None):
    q = FakeQuery(data, user_id)
    await bot._on_select(q, ctx or FakeContext())
    return q


# ---------------------------------------------------------------- 管理员测本机场订阅

def test_admin_full_flow_pick_sub_backend_sort(tmp_path):
    async def run():
        bot = make_bot(tmp_path, share_url="https://scp.example/share/{uuid}")
        msg, ctx = await speed(bot)
        status = msg.replies[-1][2]
        text, markup = status.edits[-1]
        assert "选择要测速的订阅" in text and buttons(markup) == ["3399", "IPLC", "❌ 终止操作"]
        sid = next(iter(bot.selections))

        await click(bot, f"sel:{sid}:u:0", ctx=ctx)
        text, markup = status.edits[-1]
        assert "选择测速后端" in text and "任务：<b>3399</b>" in text
        assert buttons(markup) == ["🤖 自动选择", "上海电信@2Gbps (SHCT)", "东莞电信@1Gbps (DGCT)", "❌ 终止操作"]

        await click(bot, f"sel:{sid}:b:0", ctx=ctx)
        text, markup = status.edits[-1]
        assert "选择排序方式" in text and "选中后端：<b>SHCT</b>" in text
        assert buttons(markup) == ["📋 订阅顺序（默认）", "🀄 节点名（升序）", "🚀 平均速度（升序）", "🚀 平均速度（降序）",
                                   "❌ 终止操作"]
        assert bot.api.submitted is None

        await click(bot, f"sel:{sid}:o:3", ctx=ctx)
        name, nodes, matrices = bot.api.submitted
        assert name.startswith("3399 · 测速") and nodes[0]["Name"] == "HK" and bot.api.slave_id == "SHCT"
        assert [m["Type"] for m in matrices] == ["TEST_PING_RTT", "SPEED_AVERAGE", "SPEED_MAX", "SPEED_PER_SECOND"]

        await ctx.run_tasks()
        assert bot.api.sort == "avg_speed_desc"
        caption = status.photos[0]
        assert "✅ 任务 <b>3399</b> 已完成" in caption and "<a>u9</a>" in caption and "今日剩余" not in caption
        assert status.photo_markups[0].inline_keyboard[0][0].url == "https://scp.example/share/share-1"
        assert bot.quota.used(ADMIN) == 0 and bot.running[-100] == set()

    asyncio.run(run())


def test_admin_named_sub_without_pickers(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        _, ctx = await speed(bot, "/speed iplc")  # 订阅名不区分大小写
        assert bot.api.submitted[0].startswith("IPLC · 测速") and bot.api.slave_id is None
        await ctx.run_tasks()
        assert bot.api.sort == "avg_speed_desc"

    asyncio.run(run())


def test_admin_terminate_and_owner_only(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        msg, ctx = await speed(bot)
        sid = next(iter(bot.selections))
        q = await click(bot, f"sel:{sid}:u:0", user_id=1)
        assert q.answers == ["只有发起人可以操作。"] and bot.selections[sid].sub is None
        await click(bot, f"sel:{sid}:x", ctx=ctx)
        assert "已终止" in msg.replies[-1][2].edits[-1][0] and not bot.selections
        q = await click(bot, f"sel:{sid}:u:0")
        assert "过期" in q.answers[0]

    asyncio.run(run())


# ---------------------------------------------------------------- 群成员：私聊提交任意订阅

def test_member_speed_in_group_points_to_private_chat(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        msg, ctx = await speed(bot, "/speed 3399", user_id=1)
        text, markup, _ = msg.replies[-1]
        assert "私聊中发送订阅链接" in text and not bot.selections and bot.api.submitted is None
        assert markup.inline_keyboard[0][0].url == "https://t.me/speedbot?start=g-100"
        await ctx.run_tasks()

        msg, ctx = await speed(bot, "/speed https://a.com/sub?token=1", user_id=1)
        assert msg.deleted and "已删除" in ctx.bot.sent[-1][1]
        await ctx.run_tasks()

    asyncio.run(run())


def test_start_deep_link_requires_membership(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        upd, dm = update("/start", user_id=1, chat_id=1, chat_type="private")
        await bot.cmd_start(upd, FakeContext(["g-100"]))
        assert bot.dm_targets[1].chat_id == -100 and "测速群" in dm.replies[-1][0]

        upd, dm = update("/start", user_id=3, chat_id=3, chat_type="private")
        await bot.cmd_start(upd, FakeContext(["g-100"]))
        assert 3 not in bot.dm_targets and "不是该群成员" in dm.replies[-1][0]

        upd, dm = update("/start", user_id=1, chat_id=1, chat_type="private")
        await bot.cmd_start(upd, FakeContext(["g-999"]))
        assert "未授权" in dm.replies[-1][0]

    asyncio.run(run())


async def member_submit(bot, ctx, text="trojan://pw@other.com:443#别家节点", user_id=1):
    upd, dm = update(text, user_id=user_id, chat_id=user_id, chat_type="private")
    await bot.on_private_text(upd, ctx)
    return dm


def test_member_private_flow_posts_result_to_group(tmp_path):
    async def run():
        bot = make_bot(tmp_path)  # 只有一个授权群时，私聊直接发链接即可，无需先点按钮
        ctx = FakeContext()
        dm = await member_submit(bot, ctx)
        status = dm.replies[-1][2]
        text, markup = status.edits[-1]
        assert "选择测速后端" in text and "任务：<b>群友订阅</b>" in text and "结果将发送到群「测速群」" in text
        sid = next(iter(bot.selections))
        await click(bot, f"sel:{sid}:b:1", user_id=1, ctx=ctx)
        await click(bot, f"sel:{sid}:o:0", user_id=1, ctx=ctx)

        name, nodes, _ = bot.api.submitted
        assert name.startswith("群友订阅 · 测速") and nodes[0]["Name"] == "别家节点" and bot.api.slave_id == "DGCT"
        assert "已提交" in status.edits[-1][0]
        chat_id, text, _, group_msg = ctx.bot.sent[-1]
        assert chat_id == -100 and "<a>u1</a>" in text and "今日剩余 2 次" in text

        await ctx.run_tasks()
        assert bot.api.sort is None  # 订阅顺序
        assert group_msg.photos and "<a>u1</a>" in group_msg.photos[0] and not status.photos
        assert bot.quota.used(1) == 1

    asyncio.run(run())


def test_member_daily_limit_and_admin_unlimited(tmp_path):
    async def run():
        bot = make_bot(tmp_path, daily_limit=1, backend_select=False, sort_select=False)
        ctx = FakeContext()
        await member_submit(bot, ctx)
        await ctx.run_tasks()
        assert bot.quota.used(1) == 1

        bot.api.submitted = None
        dm = await member_submit(bot, ctx)
        assert "1 次测速已用完" in dm.replies[-1][0] and bot.api.submitted is None

        for _ in range(3):  # 管理员私聊测其他订阅也不限次数
            bot.api.submitted = None
            await member_submit(bot, ctx, user_id=ADMIN)
            assert bot.api.submitted
            await ctx.run_tasks()
        assert DailyQuota(str(tmp_path / "usage.json"), 1).remaining(1) == 0  # 持久化

    asyncio.run(run())


def test_private_without_target_and_non_link_text(tmp_path):
    async def run():
        bot = make_bot(tmp_path, allowed_chat_ids={-100, -200})  # 多个群时必须先点按钮
        dm = await member_submit(bot, FakeContext())
        assert "点击「🔒 私聊发送订阅」" in dm.replies[-1][0] and not bot.selections

        bot = make_bot(tmp_path)
        dm = await member_submit(bot, FakeContext(), text="你好")
        assert "请发送订阅链接" in dm.replies[-1][0]

        dm = await member_submit(bot, FakeContext(), user_id=3)  # 非群成员
        assert "点击「🔒 私聊发送订阅」" in dm.replies[-1][0]

    asyncio.run(run())


def test_group_sub_link_deleted(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        upd, msg = update("我的订阅 https://airport.example/api/v1/client/subscribe?token=abc")
        ctx = FakeContext()
        with pytest.raises(ApplicationHandlerStop):
            await bot.on_group_message(upd, ctx)
        chat_id, text, markup, _ = ctx.bot.sent[0]
        assert msg.deleted and "已删除" in text and markup.inline_keyboard[0][0].text == "🔒 私聊发送订阅"

        upd, normal = update("看看 https://github.com/foo/bar")
        await bot.on_group_message(upd, ctx)
        assert not normal.deleted
        await ctx.run_tasks()

    asyncio.run(run())


# ---------------------------------------------------------------- 每日自动测速与手动触发

def test_seconds_until_next_run(tmp_path):
    bot = make_bot(tmp_path, schedule_times=[(9, 0), (21, 0)])
    tz = ZoneInfo("Asia/Shanghai")
    assert bot.seconds_until_next_run(datetime(2026, 10, 6, 8, 59, tzinfo=tz)) == 60
    assert bot.seconds_until_next_run(datetime(2026, 10, 6, 9, 0, tzinfo=tz)) == 12 * 3600
    assert bot.seconds_until_next_run(datetime(2026, 10, 6, 22, 0, tzinfo=tz)) == 11 * 3600
    assert make_bot(tmp_path).seconds_until_next_run() is None


def test_run_auto_tests_every_subscription_in_turn(tmp_path):
    async def run():
        bot = make_bot(tmp_path, auto_slave_id="DGCT", share_url="https://s/{uuid}")
        app = FakeApp()
        assert await bot.run_auto(app, [-100], "🕘 每日自动测速")
        assert bot.api.calls == ["3399 · 测速 · 自动测速", "IPLC · 测速 · 自动测速"]
        assert bot.api.slave_id == "DGCT" and bot.api.sort == "avg_speed_desc"
        first = app.bot.sent[0][3]
        assert "🕘 每日自动测速 · 节点 1 个" in first.photos[0] and first.photo_markups[0]
        assert not app.tasks  # 自动测速同步等待完成，不留后台任务

    asyncio.run(run())


def test_autotest_command(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        upd, msg = update("/autotest", user_id=1)
        await bot.cmd_autotest(upd, FakeContext())
        assert "只有管理员" in msg.replies[-1][0]

        upd, msg = update("/autotest", user_id=ADMIN)
        ctx = FakeContext()
        await bot.cmd_autotest(upd, ctx)
        assert "开始测速本机场订阅：3399、IPLC" in msg.replies[-1][0]
        await ctx.run_tasks()
        assert len(bot.api.calls) == 2 and "手动测速" in ctx.bot.sent[0][1]

        async with bot._auto_lock:  # 正在进行时不重复触发
            upd, msg = update("/autotest", user_id=ADMIN)
            await bot.cmd_autotest(upd, FakeContext())
            assert "正在进行中" in msg.replies[-1][0]

    asyncio.run(run())


# ---------------------------------------------------------------- 其他

def test_sub_command(tmp_path):
    async def run():
        bot = make_bot(tmp_path, schedule_times=[(9, 0)])
        upd, msg = update("/sub")
        await bot.cmd_sub(upd, FakeContext())
        text = msg.replies[-1][0]
        assert "<code>3399</code>" in text and "trojan" not in text and "每天 09:00 自动测速" in text
        assert "还可以测速 3/3 次" in text

    asyncio.run(run())


def test_progress_text():
    v = TaskView("tid", "3399", "发起人 @u · 节点 2 个\nID <code>tid</code>", "自动选择", None)
    assert SpeedBot._task_text(v, "pending").startswith("⏳ 任务 <b>3399</b> 准备中…")
    running = SpeedBot._task_text(v, "running", 1, 4, backend="上海电信")
    assert running.startswith("⚡ 任务 <b>3399</b> 进行中…\n<code>[████░░░░░░░░░░░░]</code> 25%")
    assert "后端 上海电信 · 发起人" in running


def test_quota_resets_daily(tmp_path):
    q = DailyQuota(str(tmp_path / "u.json"), 3)
    q._today = lambda: "2026-10-05"
    assert [q.consume(1) for _ in range(3)] == [2, 1, 0] and q.remaining(1) == 0 and q.remaining(2) == 3
    q._today = lambda: "2026-10-06"
    assert q.remaining(1) == 3
    assert DailyQuota(str(tmp_path / "x.json"), 0).consume(1) is None  # 0 表示不限


def test_load_subscriptions_and_times(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("subscriptions:\n  - name: 3399\n    url: https://a.com/sub\n", encoding="utf-8")
    assert load_subscriptions(str(p)) == [("3399", "https://a.com/sub")]  # 数字名称转为字符串
    p.write_text("subscriptions:\n  - name: a\n    url: x\n  - name: a\n    url: y\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="重复"):
        load_subscriptions(str(p))
    assert load_subscriptions(str(tmp_path / "missing.yaml")) == []  # 没有本机场订阅时只提供群友测速
    assert parse_times("21:00, 9:05,21:00") == [(9, 5), (21, 0)]
    with pytest.raises(SystemExit, match="格式错误"):
        parse_times("25:00")
