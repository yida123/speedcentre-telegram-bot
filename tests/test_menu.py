import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from bot.config import Config, load_subscriptions
from bot.formatter import build_plan
from bot.main import SpeedBot, TaskView
from bot.quota import DailyQuota

SUBS = [("3399", "trojan://pw@hk.com:443#HK"), ("IPLC", "trojan://pw@jp.com:443#JP")]
TASK_ID = "11111111-1111-1111-1111-111111111111"


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
        self.submitted = self.slave_id = self.sort = self.shared = None

    async def list_backends(self):
        return [
            {"client_id": "SHCT", "display_name": "上海电信@2Gbps", "is_online": True, "allow_public_access": True},
            {"client_id": "DGCT", "display_name": "东莞电信@1Gbps", "is_online": True},
            {"client_id": "US", "display_name": "美国", "is_online": False},
            {"client_id": "PRIV", "display_name": "私有", "is_online": True, "allow_public_access": False},
        ]

    async def submit_task(self, name, nodes, matrices, slave_id=None):
        self.submitted, self.slave_id = (name, nodes, matrices), slave_id
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

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        m = FakeMessage(chat_id)
        self.sent.append((chat_id, text, m))
        return m


class FakeApp:
    def __init__(self):
        self.tasks = []
        self.bot = FakeBot()

    def create_task(self, coro, name=None):
        self.tasks.append(coro)


class FakeContext:
    def __init__(self, args=None):
        self.application = FakeApp()
        self.bot = self.application.bot
        self.args = args or []

    async def run_tasks(self):
        for t in self.application.tasks:
            await t
        self.application.tasks = []


def make_bot(tmp_path, **kw):
    kw.setdefault("subscriptions", SUBS)
    kw.setdefault("allowed_chat_ids", {-100})
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


async def speed(bot, text="/speed", user_id=1):
    upd, msg = update(text, user_id)
    ctx = FakeContext(text.split()[1:])
    await bot.cmd_speed(upd, ctx)
    return msg, ctx


async def click(bot, data, user_id=1, ctx=None):
    q = FakeQuery(data, user_id)
    await bot._on_select(q, ctx or FakeContext())
    return q


def test_full_flow_pick_sub_backend_sort(tmp_path):
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
        assert sid not in bot.selections

        await ctx.run_tasks()
        assert bot.api.sort == "avg_speed_desc"
        caption = status.photos[0]
        assert "✅ 任务 <b>3399</b> 已完成" in caption and "<a>u1</a>" in caption and "今日剩余 2 次" in caption
        assert status.photo_markups[0].inline_keyboard[0][0].url == "https://scp.example/share/share-1"
        assert bot.running[-100] == set()

    asyncio.run(run())


def test_named_sub_without_pickers_uses_default_sort(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        msg, ctx = await speed(bot, "/speed iplc")  # 订阅名不区分大小写
        assert bot.api.submitted[0].startswith("IPLC · 测速") and bot.api.slave_id is None
        await ctx.run_tasks()
        assert bot.api.sort == "avg_speed_desc"

    asyncio.run(run())


def test_single_sub_and_subscription_order(tmp_path):
    async def run():
        bot = make_bot(tmp_path, subscriptions=SUBS[:1])
        msg, ctx = await speed(bot)
        status = msg.replies[-1][2]
        assert "选择测速后端" in status.edits[-1][0]  # 只有一个订阅时跳过订阅选择
        sid = next(iter(bot.selections))
        await click(bot, f"sel:{sid}:b:auto", ctx=ctx)
        await click(bot, f"sel:{sid}:o:0", ctx=ctx)  # 订阅顺序
        await ctx.run_tasks()
        assert bot.api.slave_id is None and bot.api.sort is None

    asyncio.run(run())


def test_daily_limit_and_admin_unlimited(tmp_path):
    async def run():
        bot = make_bot(tmp_path, daily_limit=1, admin_user_ids={9}, backend_select=False, sort_select=False)
        _, ctx = await speed(bot, "/speed 3399")
        assert bot.api.submitted
        await ctx.run_tasks()

        bot.api.submitted = None
        msg, _ = await speed(bot, "/speed 3399")
        assert "1 次测速已用完" in msg.replies[-1][0] and bot.api.submitted is None

        for _ in range(3):  # 管理员不限次数
            bot.api.submitted = None
            _, ctx = await speed(bot, "/speed 3399", user_id=9)
            assert bot.api.submitted
            await ctx.run_tasks()

        # 次数持久化：重启后仍然记得
        assert DailyQuota(str(tmp_path / "usage.json"), 1).remaining(1) == 0

    asyncio.run(run())


def test_terminate_does_not_consume_quota(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        msg, ctx = await speed(bot)
        sid = next(iter(bot.selections))
        await click(bot, f"sel:{sid}:x", ctx=ctx)
        assert "已终止" in msg.replies[-1][2].edits[-1][0] and not bot.selections
        assert bot.quota.remaining(1) == 3

    asyncio.run(run())


def test_only_owner_can_click_and_expiry(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        await speed(bot)
        sid = next(iter(bot.selections))
        q = await click(bot, f"sel:{sid}:u:0", user_id=2)
        assert q.answers == ["只有发起人可以操作。"] and bot.selections[sid].sub is None
        bot.selections[sid].created -= 10_000
        q = await click(bot, f"sel:{sid}:u:0")
        assert "过期" in q.answers[0]

    asyncio.run(run())


def test_rejects_links_unknown_subs_and_private(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        msg, ctx = await speed(bot, "/speed https://a.com/sub?token=1")
        assert msg.deleted and "只测速固定订阅" in ctx.bot.sent[-1][1] and bot.api.submitted is None

        msg, _ = await speed(bot, "/speed nope")
        assert "未找到订阅" in msg.replies[-1][0] and "<code>3399</code>" in msg.replies[-1][0]

        upd, msg = update("/speed", chat_type="private", chat_id=1)
        await bot.cmd_speed(upd, FakeContext())
        assert "群组" in msg.replies[-1][0] and not bot.selections

        upd, msg = update("/speed", chat_id=-200)
        await bot.cmd_speed(upd, FakeContext())
        assert "未授权" in msg.replies[-1][0]

    asyncio.run(run())


def test_group_sub_link_deleted(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        upd, msg = update("我的订阅 https://airport.example/api/v1/client/subscribe?token=abc")
        ctx = FakeContext()
        with pytest.raises(ApplicationHandlerStop):
            await bot.on_group_message(upd, ctx)
        assert msg.deleted and "已删除" in ctx.bot.sent[0][1]

        upd, normal = update("看看 https://github.com/foo/bar")
        await bot.on_group_message(upd, ctx)
        assert not normal.deleted
        for t in ctx.application.tasks:  # 提示消息的定时删除协程
            t.close()

    asyncio.run(run())


def test_sub_command_lists_names_and_quota(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        upd, msg = update("/sub")
        await bot.cmd_sub(upd, FakeContext())
        text = msg.replies[-1][0]
        assert "<code>3399</code>" in text and "<code>IPLC</code>" in text and "trojan" not in text
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


def test_load_subscriptions(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("subscriptions:\n  - name: 3399\n    url: https://a.com/sub\n", encoding="utf-8")
    assert load_subscriptions(str(p)) == [("3399", "https://a.com/sub")]  # 数字名称转为字符串
    p.write_text("subscriptions:\n  - name: a\n    url: x\n  - name: a\n    url: y\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="重复"):
        load_subscriptions(str(p))
    with pytest.raises(SystemExit, match="找不到"):
        load_subscriptions(str(tmp_path / "missing.yaml"))
