import asyncio

from bot.config import Config
from bot.formatter import build_plan
from bot.main import Selection, SpeedBot


def test_build_plan():
    plan = build_plan("x", {"rtt", "speed", "geo"}, ("netflix",))
    types = [m["Type"] for m in plan.matrices]
    assert types == ["TEST_PING_RTT", "SPEED_AVERAGE", "SPEED_MAX", "SPEED_PER_SECOND",
                     "GEOIP_INBOUND", "GEOIP_OUTBOUND", "TEST_SCRIPT"]
    assert plan.matrices[-1]["Params"] == "INTERNAL::netflix"
    assert plan.views == ("normalview", "topologyview") and plan.sort == "avg_speed_desc"
    assert build_plan("t", {"geo"}).views == ("topologyview",)


class FakeUser:
    def __init__(self, uid):
        self.id, self.full_name = uid, f"u{uid}"

    def mention_html(self):
        return f"<a>u{self.id}</a>"


class FakeMessage:
    def __init__(self, chat_id=-100):
        self.chat_id = chat_id
        self.edits, self.photos, self.deleted = [], [], False

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw.get("reply_markup")))

    async def reply_photo(self, photo, caption=None, **kw):
        self.photos.append(caption)


class FakeQuery:
    def __init__(self, data, uid):
        self.data, self.from_user, self.answers = data, FakeUser(uid), []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class FakeAPI:
    def __init__(self):
        self.submitted = None

    async def list_scripts(self):
        return [{"id": "nf", "name": "Netflix", "type": "media"}, {"id": "ip1", "name": "IP", "type": "ip"}]

    async def list_backends(self):
        return [
            {"client_id": "hk", "display_name": "香港 HKT", "is_online": True, "allow_public_access": True,
             "speed_pending": 3, "conn_pending": 0},
            {"client_id": "jp", "display_name": "日本 IIJ", "is_online": True, "speed_pending": 0, "conn_pending": 1},
            {"client_id": "us", "display_name": "美国", "is_online": False},
            {"client_id": "priv", "display_name": "私有", "is_online": True, "allow_public_access": False},
        ]

    async def submit_task(self, name, nodes, matrices, slave_id=None):
        self.submitted = (name, matrices)
        self.slave_id = slave_id
        return {"task_id": "11111111-1111-1111-1111-111111111111", "status": "pending"}

    async def get_task(self, task_id):
        return {"status": "completed", "credit_cost": 3}

    async def get_result(self, task_id):
        return {"result": {"Results": []}}

    async def export_image(self, task_id, view, sort=None):
        return b"img-" + view.encode()


class FakeBot:
    username = "speedbot"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        m = FakeMessage(chat_id)
        self.sent.append((chat_id, text, kw.get("reply_markup"), m))
        return m

    async def get_chat_member(self, chat_id, user_id):
        from types import SimpleNamespace
        return SimpleNamespace(status="member" if user_id == 1 else "left")

    async def get_chat(self, chat_id):
        from types import SimpleNamespace
        return SimpleNamespace(title="测速群")


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


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_menu_flow():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k", admin_user_ids={99}))
        bot.api = FakeAPI()
        status = FakeMessage()
        sel = Selection(owner=FakeUser(1), chat_id=-100, target_title="g", status=status, nodes=[{"Name": "n"}],
                        skipped=0,
                        name_filter=None, slave=None, options={"rtt"})
        bot.selections["ab"] = sel
        ctx = FakeContext()

        await bot._on_select(FakeQuery("sel:ab:t:speed", 1), ctx)
        assert sel.options == {"rtt", "speed"}
        assert "✅ 测速" in buttons(status.edits[-1][1])

        q = FakeQuery("sel:ab:t:udp", 2)  # 非发起人不能操作
        await bot._on_select(q, ctx)
        assert sel.options == {"rtt", "speed"} and q.answers == ["只有发起人可以操作。"]

        await bot._on_select(FakeQuery("sel:ab:scripts", 1), ctx)
        assert sel.script_list == [("nf", "Netflix")]
        await bot._on_select(FakeQuery("sel:ab:s:0", 1), ctx)
        assert sel.scripts == {"nf"}
        await bot._on_select(FakeQuery("sel:ab:t:geo", 99), ctx)  # 管理员可以操作
        await bot._on_select(FakeQuery("sel:ab:go", 1), ctx)

        name, matrices = bot.api.submitted
        assert name.startswith("TG 自定义测试")
        assert {"Type": "TEST_SCRIPT", "Params": "INTERNAL::nf"} in matrices
        assert "ab" not in bot.selections

        await ctx.application.tasks[0]
        assert len(status.photos) == 2 and status.photos[1] is None  # normalview + topologyview
        assert bot.running[-100] == set()

        q = FakeQuery("sel:ab:go", 1)  # 已提交后菜单失效
        await bot._on_select(q, ctx)
        assert "过期" in q.answers[0]

    asyncio.run(run())


def test_dm_submission_posts_to_group():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k"))
        bot.api = FakeAPI()
        dm_status = FakeMessage(chat_id=1)
        sel = Selection(owner=FakeUser(1), chat_id=-100, target_title="测速群", status=dm_status,
                        nodes=[{"Name": "n"}], skipped=0, name_filter=None, slave=None, options={"rtt"})
        bot.selections["cd"] = sel
        ctx = FakeContext()
        await bot._on_select(FakeQuery("sel:cd:go", 1), ctx)

        assert "已提交" in dm_status.edits[-1][0] and "测速群" in dm_status.edits[-1][0]
        chat_id, text, _, group_msg = ctx.bot.sent[0]
        assert chat_id == -100 and "<a>u1</a>" in text  # 群消息 @ 发起人
        await ctx.application.tasks[0]
        assert group_msg.photos and "<a>u1</a>" in group_msg.photos[0]  # 结果图发在群里
        assert not dm_status.photos

    asyncio.run(run())


class FakeUpdate:
    def __init__(self, msg, chat_id, chat_type, user):
        from types import SimpleNamespace
        self.effective_message = msg
        self.effective_chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.effective_user = user


def test_group_sub_link_deleted_and_redirected():
    from telegram.ext import ApplicationHandlerStop
    import pytest

    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k", allowed_chat_ids={-100}))
        ctx = FakeContext()
        user = FakeUser(1)
        user.is_bot = False

        msg = FakeMessage()
        msg.text, msg.caption = "我的订阅 https://airport.example/api/v1/client/subscribe?token=abc", None
        with pytest.raises(ApplicationHandlerStop):
            await bot.on_group_message(FakeUpdate(msg, -100, "supergroup", user), ctx)
        assert msg.deleted
        chat_id, text, markup, _ = ctx.bot.sent[0]
        assert chat_id == -100 and "已删除" in text
        assert markup.inline_keyboard[0][0].url == "https://t.me/speedbot?start=g-100_test"

        normal = FakeMessage()
        normal.text, normal.caption = "看看 https://github.com/foo/bar", None
        await bot.on_group_message(FakeUpdate(normal, -100, "supergroup", user), ctx)
        assert not normal.deleted
        for t in ctx.application.tasks:  # 引导消息的定时删除协程
            t.close()

    asyncio.run(run())


def test_start_deep_link_requires_membership():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k", allowed_chat_ids={-100}))
        replies = []

        class DM(FakeMessage):
            async def reply_text(self, text, **kw):
                replies.append(text)

        await bot.cmd_start(FakeUpdate(DM(1), 1, "private", FakeUser(1)), FakeContext(["g-100_speed"]))
        assert bot.dm_targets[1].chat_id == -100 and bot.dm_targets[1].command == "speed"
        assert "测速群" in replies[-1]

        await bot.cmd_start(FakeUpdate(DM(2), 2, "private", FakeUser(2)), FakeContext(["g-100_test"]))
        assert 2 not in bot.dm_targets and "不是该群成员" in replies[-1]

        await bot.cmd_start(FakeUpdate(DM(1), 1, "private", FakeUser(1)), FakeContext(["g-999_test"]))
        assert "未授权" in replies[-1]

    asyncio.run(run())


def test_selectable_backends_filter_and_order():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k"))
        bot.api = FakeAPI()
        backends = await bot._selectable_backends()
        assert [b["client_id"] for b in backends] == ["jp", "hk"]  # 离线、不允许调用的被排除，按排队数排序
        assert SpeedBot._match_backend(backends, "香港 hkt")["client_id"] == "hk"
        assert SpeedBot._match_backend(backends, "us") is None

        bot = SpeedBot(Config(bot_token="t", api_key="k", allowed_backends={"hk"}))
        bot.api = FakeAPI()
        assert [b["client_id"] for b in await bot._selectable_backends()] == ["hk"]

    asyncio.run(run())


def make_sel(bot, **kw):
    return Selection(owner=FakeUser(1), chat_id=-100, target_title="g", status=FakeMessage(), nodes=[{"Name": "n"}],
                     skipped=0, name_filter=None, slave=None, **kw)


def test_quick_command_picks_backend_then_submits():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k"))
        bot.api = FakeAPI()
        sel = make_sel(bot, options={"rtt", "speed"}, backends=await bot._selectable_backends(), quick="speed",
                       page="backends")
        bot.selections["q1"] = sel
        ctx = FakeContext()

        await bot._render_menu("q1", sel)
        labels = buttons(sel.status.edits[-1][1])
        assert labels[0] == "✅ 🤖 自动选择（推荐）" and "🟢 日本 IIJ · 排队 1" in labels

        await bot._on_select(FakeQuery("sel:q1:b:1", 1), ctx)  # 第 2 个：香港
        assert bot.api.slave_id == "hk" and bot.api.submitted[0].startswith("TG 测速")
        assert any("后端：香港 HKT" in text for text, _ in sel.status.edits)
        for t in ctx.application.tasks:
            t.close()

    asyncio.run(run())


def test_menu_backend_page_and_pagination():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k"))
        bot.api = FakeAPI()
        many = [{"client_id": f"b{i}", "display_name": f"B{i}", "is_online": True} for i in range(10)]

        async def list_backends():
            return many
        bot.api.list_backends = list_backends
        sel = make_sel(bot, options={"rtt"})
        bot.selections["m1"] = sel
        ctx = FakeContext()

        await bot._on_select(FakeQuery("sel:m1:backends", 1), ctx)
        labels = buttons(sel.status.edits[-1][1])
        assert "1/2" in labels and "下一页 ▸" in labels and "◂ 返回" in labels
        await bot._on_select(FakeQuery("sel:m1:bp:1", 1), ctx)
        assert "🟢 B9 · 排队 0" in buttons(sel.status.edits[-1][1])
        await bot._on_select(FakeQuery("sel:m1:b:9", 1), ctx)
        assert sel.slave == "b9" and sel.page == "main"
        assert "🖥 后端：B9 ▸" in buttons(sel.status.edits[-1][1])

        await bot._on_select(FakeQuery("sel:m1:go", 1), ctx)
        assert bot.api.slave_id == "b9"
        for t in ctx.application.tasks:
            t.close()

    asyncio.run(run())
