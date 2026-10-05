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
    def __init__(self):
        self.edits, self.photos = [], []

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

    async def submit_task(self, name, nodes, matrices, slave_id=None):
        self.submitted = (name, matrices)
        return {"task_id": "11111111-1111-1111-1111-111111111111", "status": "pending"}

    async def get_task(self, task_id):
        return {"status": "completed", "credit_cost": 3}

    async def get_result(self, task_id):
        return {"result": {"Results": []}}

    async def export_image(self, task_id, view, sort=None):
        return b"img-" + view.encode()


class FakeApp:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro, name=None):
        self.tasks.append(coro)


class FakeContext:
    def __init__(self):
        self.application = FakeApp()


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_menu_flow():
    async def run():
        bot = SpeedBot(Config(bot_token="t", api_key="k", admin_user_ids={99}))
        bot.api = FakeAPI()
        status = FakeMessage()
        sel = Selection(owner=FakeUser(1), chat_id=-100, status=status, nodes=[{"Name": "n"}], skipped=0,
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
