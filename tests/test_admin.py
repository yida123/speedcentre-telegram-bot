import asyncio
import json
from types import SimpleNamespace

from bot.admin import parse_duration, parse_number
from bot.main import ActiveTask, SpeedBot
from bot.settings import DailyStats
from test_menu import (
    ADMIN, SUBS, TASK_ID, FakeAPI, FakeContext, FakeMessage, FakeUser, click, make_bot, member_submit, speed, update,
)


def dm(text, user_id=ADMIN):
    return update(text, user_id=user_id, chat_id=user_id, chat_type="private")


async def command(bot, name, text, user_id=ADMIN, private=True, ctx=None, reply_to=None):
    upd, msg = dm(text, user_id) if private else update(text, user_id)
    msg.reply_to_message = reply_to
    ctx = ctx or FakeContext(text.split()[1:])
    ctx.args = text.split()[1:]
    await getattr(bot, f"cmd_{name}")(upd, ctx)
    return msg.replies[-1][0] if msg.replies else None, msg, ctx


def restart(bot, tmp_path, **kw):
    """用同一个 DATA_DIR 重新创建 bot，模拟重启。"""
    return make_bot(tmp_path, **kw)


def saved(tmp_path):
    return json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 权限

def test_admin_commands_need_admin(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        for name in ("settings", "group", "ban", "airport", "schedule", "cooldown", "status", "tasks", "stopall"):
            reply, _, _ = await command(bot, name, f"/{name}", user_id=1)
            assert reply == "只有管理员可以使用这个命令。", name
        # 普通管理员不能管理管理员
        bot.cfg.extra_admin_ids = {5}
        reply, _, _ = await command(bot, "admin", "/admin add 6", user_id=5)
        assert "只有超级管理员" in reply and bot.cfg.extra_admin_ids == {5}

    asyncio.run(run())


def test_help_shows_admin_commands_only_to_admins_in_private(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        upd, msg = dm("/help")
        await bot.cmd_help(upd, FakeContext())
        assert "/schedule" in msg.replies[-1][0]
        upd, msg = dm("/help", user_id=1)
        await bot.cmd_help(upd, FakeContext())
        assert "/schedule" not in msg.replies[-1][0]
        upd, msg = update("/help", user_id=ADMIN)
        await bot.cmd_help(upd, FakeContext())
        assert "/schedule" not in msg.replies[-1][0]

    asyncio.run(run())


def test_set_commands_gives_admins_their_own_menu(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        ctx = FakeContext()
        await bot.set_commands(ctx.bot)
        assert "schedule" not in ctx.bot.commands[None] and "speed" in ctx.bot.commands[None]
        assert "schedule" in ctx.bot.commands[ADMIN]
        await command(bot, "admin", "/admin add 5", ctx=ctx)
        assert "stopall" in ctx.bot.commands[5]
        await command(bot, "admin", "/admin del 5", ctx=ctx)
        assert 5 not in ctx.bot.commands

    asyncio.run(run())


# ---------------------------------------------------------------- 授权群、管理员、封禁

def test_group_whitelist_add_remove_and_persist(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        reply, _, _ = await command(bot, "group", "/group add -200")
        assert "已添加授权群" in reply and bot.cfg.allowed_chat_ids == {-100, -200}
        reply, _, _ = await command(bot, "group", "/group add 123")
        assert "请提供群组 ID" in reply
        # 在要授权的群里直接发 /group add
        reply, _, _ = await command(bot, "group", "/group add", private=False)
        assert "已经是授权群" in reply
        reply, _, _ = await command(bot, "group", "/group del -100")
        assert "已移除" in reply and bot.cfg.allowed_chat_ids == {-200}
        reply, _, _ = await command(bot, "group", "/group del -200")
        assert "最后一个授权群" in reply and bot.cfg.allowed_chat_ids == {-200}
        assert saved(tmp_path)["allowed_chat_ids"] == [-200]
        assert restart(bot, tmp_path).cfg.allowed_chat_ids == {-200}  # 重启后 settings.json 覆盖 .env
        assert bot.schedule_changed.is_set()

    asyncio.run(run())


def test_removed_group_no_longer_receives_member_tests(tmp_path):
    async def run():
        bot = make_bot(tmp_path, allowed_chat_ids={-100, -200, -300}, backend_select=False, sort_select=False)
        ctx = FakeContext()
        upd, _ = dm("/start", user_id=1)
        await bot.cmd_start(upd, FakeContext(["g-200"]))
        assert bot.dm_targets[1].chat_id == -200
        await command(bot, "group", "/group del -200")
        out = await member_submit(bot, ctx)
        assert "请先在机场群里发送 /speed" in out.replies[-1][0] and bot.api.submitted is None

    asyncio.run(run())


def test_add_and_remove_admins(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        reply, _, _ = await command(bot, "admin", "/admin add 5")
        assert "已添加管理员" in reply and bot.is_admin(5) and not bot.is_super(5)
        # 新管理员可以手动测速、修改设置
        reply, _, _ = await command(bot, "cooldown", "/cooldown 60", user_id=5)
        assert bot.cfg.cooldown_seconds == 60
        # 在群里回复某人的消息添加
        target = FakeMessage()
        target.from_user = SimpleNamespace(id=6, is_bot=False)
        reply, _, _ = await command(bot, "admin", "/admin add", private=False, reply_to=target)
        assert bot.is_admin(6)
        reply, _, _ = await command(bot, "admin", f"/admin del {ADMIN}")
        assert "不能用命令移除" in reply and bot.is_admin(ADMIN)
        await command(bot, "admin", "/admin del 5")
        assert not bot.is_admin(5) and restart(bot, tmp_path).cfg.extra_admin_ids == {6}

    asyncio.run(run())


def test_banned_member_cannot_test(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        reply, _, _ = await command(bot, "ban", f"/ban {ADMIN}")
        assert "不能禁止管理员" in reply
        reply, _, _ = await command(bot, "ban", "/ban 1")
        assert "已禁止" in reply and bot.is_banned(1)

        ctx = FakeContext()
        out = await member_submit(bot, ctx)
        assert "禁止" in out.replies[-1][0] and bot.api.submitted is None
        msg, _ = await speed(bot, user_id=1)
        assert "禁止" in msg.replies[-1][0]
        upd, out = dm("/start", user_id=1)
        await bot.cmd_start(upd, FakeContext(["g-100"]))
        assert "禁止" in out.replies[-1][0]

        await command(bot, "unban", "/unban 1")
        assert not bot.is_banned(1)
        await member_submit(bot, ctx)
        assert bot.api.submitted
        await ctx.run_tasks()

    asyncio.run(run())


def test_ban_while_menu_open_blocks_submission(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        ctx = FakeContext()
        out = await member_submit(bot, ctx)
        sid = next(iter(bot.selections))
        await command(bot, "ban", "/ban 1")
        await click(bot, f"sel:{sid}:b:auto", user_id=1, ctx=ctx)
        await click(bot, f"sel:{sid}:o:0", user_id=1, ctx=ctx)
        assert bot.api.submitted is None and "禁止" in out.replies[-1][2].edits[-1][0]
        await ctx.run_tasks()

    asyncio.run(run())


# ---------------------------------------------------------------- 本机场订阅与时间表

def test_airport_add_delete_in_private_only(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        reply, msg, _ = await command(bot, "airport", "/airport add 新 https://a.example/sub?token=x", private=False)
        assert "请在私聊里添加" in reply and msg.deleted and len(bot.cfg.subscriptions) == 2
        reply, _, _ = await command(bot, "airport", "/airport add 新 https://a.example/sub?token=x")
        assert "已添加" in reply and bot.cfg.subscriptions[-1] == ("新", "https://a.example/sub?token=x")
        reply, _, _ = await command(bot, "airport", "/airport add 3399 https://b.example/sub")
        assert "已经有名为" in reply
        reply, _, _ = await command(bot, "airport", "/airport add x 不是链接")
        assert "没有找到订阅链接" in reply
        reply, _, _ = await command(bot, "airport", "/airport del iplc")
        assert "已删除" in reply and [n for n, _ in bot.cfg.subscriptions] == ["3399", "新"]
        # 群里列表只显示名称
        reply, _, _ = await command(bot, "airport", "/airport", private=False)
        assert "a.example" not in reply and "<code>新</code>" in reply
        reply, _, _ = await command(bot, "airport", "/airport")
        assert "a.example" in reply
        assert restart(bot, tmp_path).cfg.subscriptions == [SUBS[0], ("新", "https://a.example/sub?token=x")]

    asyncio.run(run())


def test_schedule_command_sets_cron_and_persists(tmp_path):
    async def run():
        bot = make_bot(tmp_path, schedule_spec="09:00")
        reply, _, _ = await command(bot, "schedule", "/schedule")
        assert "每天 09:00" in reply and "接下来" in reply and "cron" in reply
        reply, _, _ = await command(bot, "schedule", "/schedule * * * * *")
        assert "至少间隔 10 分钟" in reply and bot.schedule.spec == "09:00"
        reply, _, _ = await command(bot, "schedule", "/schedule 0 */6 * * *")
        assert "每 6 小时" in reply and bot.schedule.spec == "0 */6 * * *" and bot.schedule_changed.is_set()
        assert saved(tmp_path)["schedule_spec"] == "0 */6 * * *"
        assert restart(bot, tmp_path).schedule.spec == "0 */6 * * *"
        reply, _, _ = await command(bot, "schedule", "/schedule off")
        assert "已关闭" in reply and bot.schedule is None and bot.seconds_until_next_run() is None
        assert restart(bot, tmp_path).schedule is None

    asyncio.run(run())


def test_scheduler_wakes_up_when_schedule_changes(tmp_path):
    async def run():
        bot = make_bot(tmp_path)  # 没有时间表：定时任务等待设置变化
        runs = []

        async def fake_run_auto(app, chats, title, wait_for_lock=False):
            runs.append((chats, title))
            bot.schedule = None  # 只跑一次
            return True

        bot.run_auto = fake_run_auto
        task = asyncio.create_task(bot.scheduler(SimpleNamespace()))
        await asyncio.sleep(0.05)
        assert not runs
        await command(bot, "schedule", "/schedule 6h")
        bot.seconds_until_next_run = lambda: 0.01 if bot.schedule else None
        bot.schedule_changed.set()
        await asyncio.sleep(1.2)
        assert runs == [([-100], "🕘 定时自动测速")]
        task.cancel()

    asyncio.run(run())


def test_scheduler_waits_for_subscriptions(tmp_path):
    async def run():
        bot = make_bot(tmp_path, subscriptions=[], schedule_spec="09:00")
        task = asyncio.create_task(bot.scheduler(SimpleNamespace()))
        await asyncio.sleep(0.05)
        assert not task.done()  # 没有订阅时不退出，等管理员添加
        await command(bot, "airport", "/airport add a https://a.example/sub")
        assert bot.schedule_changed.is_set()
        task.cancel()

    asyncio.run(run())


# ---------------------------------------------------------------- 群成员限制

def test_number_settings_and_parsing(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        assert parse_duration("90") == 90 and parse_duration("5m") == 300 and parse_duration("1h") == 3600
        assert parse_duration("off") == 0 and parse_number("50%") == 50
        reply, _, _ = await command(bot, "cooldown", "/cooldown 5m")
        assert "5 分钟" in reply and bot.cfg.cooldown_seconds == 300
        reply, _, _ = await command(bot, "cooldown", "/cooldown abc")
        assert "格式错误" in reply
        reply, _, _ = await command(bot, "alert", "/alert 120")
        assert "最大不能超过 100" in reply
        await command(bot, "limit", "/limit 0")
        assert bot.cfg.daily_limit == 0 and bot.quota.limit == 0 and bot._remaining(1) is None
        await command(bot, "maxnodes", "/maxnodes 20")
        await command(bot, "creditalert", "/creditalert 5000")
        reply, _, _ = await command(bot, "pin", "/pin off")
        assert "关闭" in reply and not bot.cfg.pin_auto_result
        reply, _, _ = await command(bot, "settings", "/settings")
        assert "5 分钟" in reply and "20 个" in reply and "5000" in reply and "置顶自动测速结果：关" in reply
        cfg = restart(bot, tmp_path).cfg
        assert (cfg.cooldown_seconds, cfg.daily_limit, cfg.member_max_nodes, cfg.credit_alert, cfg.pin_auto_result) \
            == (300, 0, 20, 5000, False)

    asyncio.run(run())


def test_member_cooldown(tmp_path):
    async def run():
        bot = make_bot(tmp_path, daily_limit=0, cooldown_seconds=300, backend_select=False, sort_select=False)
        ctx = FakeContext()
        await member_submit(bot, ctx)
        assert bot.api.submitted
        await ctx.run_tasks()
        bot.api.submitted = None
        out = await member_submit(bot, ctx)
        assert "测速太频繁" in out.replies[-1][0] and "分钟后再试" in out.replies[-1][0] and bot.api.submitted is None
        for _ in range(2):  # 管理员不受冷却限制
            bot.api.submitted = None
            await member_submit(bot, ctx, user_id=ADMIN)
            assert bot.api.submitted
            await ctx.run_tasks()
        bot.last_test[1] -= 301
        await member_submit(bot, ctx)
        assert bot.api.submitted
        await ctx.run_tasks()

    asyncio.run(run())


def test_failed_submission_does_not_start_cooldown(tmp_path):
    async def run():
        from bot.api import APIError

        bot = make_bot(tmp_path, daily_limit=0, cooldown_seconds=300, backend_select=False, sort_select=False)

        async def fail(*a, **kw):
            raise APIError("积分不足")

        bot.api.submit_task = fail
        ctx = FakeContext()
        await member_submit(bot, ctx)
        assert 1 not in bot.last_test and bot._cooldown_left(1) == 0
        await ctx.run_tasks()

    asyncio.run(run())


def test_member_node_cap(tmp_path):
    async def run():
        bot = make_bot(tmp_path, member_max_nodes=2, backend_select=False, sort_select=False)
        links = " ".join(f"trojan://pw@n{i}.com:443#N{i}" for i in range(5))
        ctx = FakeContext()
        await member_submit(bot, ctx, text=links)
        assert len(bot.api.submitted[1]) == 2
        _, text, _, _ = ctx.bot.sent[-1]
        assert "每次最多测试 2 个节点" in text
        await ctx.run_tasks()
        await member_submit(bot, ctx, text=links, user_id=ADMIN)  # 管理员不受限
        assert len(bot.api.submitted[1]) == 5
        await ctx.run_tasks()

    asyncio.run(run())


# ---------------------------------------------------------------- 统计、提醒与置顶

class ResultAPI(FakeAPI):
    """返回节点结果和积分消耗的 API。"""

    def __init__(self, speeds, cost=100, status="completed"):
        super().__init__()
        self.speeds, self.cost, self.status = speeds, cost, status

    async def get_task(self, task_id):
        return {"status": self.status, "credit_cost": self.cost, "error_msg": "后端离线"}

    async def get_result(self, task_id):
        return {"result": {"Results": [
            {"ProxyInfo": {"Name": f"N{i}"}, "Matrices": [{"Type": "SPEED_AVERAGE", "Payload": json.dumps({"Value": v})}]}
            for i, v in enumerate(self.speeds)]}}


def admin_dms(app):
    return [text for chat_id, text, _, _ in app.bot.sent if chat_id == ADMIN]


def test_credit_alert_once_per_day(tmp_path):
    async def run():
        bot = make_bot(tmp_path, credit_alert=150)
        bot.api = ResultAPI([1e6], cost=100)
        app = FakeContext().application
        await bot.run_auto(app, [-100], "🕘")  # 两个订阅，共 200 积分
        alerts = [t for t in admin_dms(app) if "💰" in t]
        assert len(alerts) == 1 and "消耗 200 积分" in alerts[0]
        assert bot.stats.today() == (2, 200)
        await bot.run_auto(app, [-100], "🕘")
        assert len([t for t in admin_dms(app) if "💰" in t]) == 1  # 当天只提醒一次
        stats = DailyStats(str(tmp_path / "stats.json"))
        assert stats.today() == (4, 400)  # 重启不丢

    asyncio.run(run())


def test_anomaly_alert_for_auto_runs(tmp_path):
    async def run():
        bot = make_bot(tmp_path, anomaly_percent=50)
        bot.api = ResultAPI([0, 0, 1e6])
        app = FakeContext().application
        await bot.run_auto(app, [-100], "🕘")
        alerts = admin_dms(app)
        assert len(alerts) == 2 and "2/3 个节点没有速度" in alerts[0] and "N0、N1" in alerts[0]

        app = FakeContext().application
        bot.api = ResultAPI([0, 1e6, 1e6])  # 1/3 < 50%
        await bot.run_auto(app, [-100], "🕘")
        assert not admin_dms(app)

        app = FakeContext().application
        bot.api = ResultAPI([], status="failed")
        await bot.run_auto(app, [-100], "🕘")
        assert len(admin_dms(app)) == 2 and "后端离线" in admin_dms(app)[0]

        # 群成员的测速不提醒
        app = FakeContext().application
        bot.api = ResultAPI([0, 0, 0])
        ctx = FakeContext(app=app)
        bot.cfg.backend_select = bot.cfg.sort_select = False
        await member_submit(bot, ctx)
        await ctx.run_tasks()
        assert not admin_dms(app)

        await command(bot, "alert", "/alert 0")
        app = FakeContext().application
        bot.api = ResultAPI([], status="failed")
        await bot.run_auto(app, [-100], "🕘")
        assert not admin_dms(app)

    asyncio.run(run())


def test_auto_results_are_pinned_and_previous_unpinned(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        app = FakeContext().application
        await bot.run_auto(app, [-100], "🕘")
        first = list(app.bot.pins)
        assert len(first) == 2 and not app.bot.unpins
        assert bot.settings.pinned(-100) == [m for _, m in first]

        bot = restart(bot, tmp_path)  # 重启后仍记得上一轮置顶的消息
        app = FakeContext().application
        await bot.run_auto(app, [-100], "🕘")
        assert app.bot.unpins == first and len(app.bot.pins) == 2 and app.bot.pins != first

        bot.cfg.pin_auto_result = False
        app = FakeContext().application
        await bot.run_auto(app, [-100], "🕘")
        assert not app.bot.pins and not app.bot.unpins

    asyncio.run(run())


def test_pin_service_message_from_bot_is_deleted(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        ctx = FakeContext()
        msg = FakeMessage()
        msg.from_user = SimpleNamespace(id=ctx.bot.id)
        await bot.on_pinned(SimpleNamespace(effective_message=msg), ctx)
        assert msg.deleted
        other = FakeMessage()
        other.from_user = SimpleNamespace(id=1)
        await bot.on_pinned(SimpleNamespace(effective_message=other), ctx)
        assert not other.deleted

    asyncio.run(run())


# ---------------------------------------------------------------- 任务管理与状态

def test_tasks_cancel_and_stopall(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        await member_submit(bot, ctx)  # 任务进行中（跟踪协程还没运行）
        assert TASK_ID in bot.active and bot.active[TASK_ID].owner_id == 1

        reply, msg, _ = await command(bot, "tasks", "/tasks")
        assert "进行中的任务</b>（1 个）" in reply and "群友订阅" in reply and "<a>u1</a>" in reply
        markup = msg.replies[-1][1]
        assert markup.inline_keyboard[0][0].callback_data == f"cancel:{TASK_ID}"

        # 别人不能取消；发起人直接 /cancel（只有一个任务时）即可
        reply, _, _ = await command(bot, "cancel", f"/cancel {TASK_ID}", user_id=2, private=False)
        assert "只有任务发起人或管理员" in reply
        reply, _, _ = await command(bot, "cancel", "/cancel", user_id=1, private=False)
        assert "已发起取消" in reply and bot.api.canceled == [TASK_ID]

        # 回复进度消息取消
        progress = FakeMessage(text=f"⚡ 任务 群友订阅 进行中…\nID {TASK_ID}")
        reply, _, _ = await command(bot, "cancel", "/cancel", private=False, reply_to=progress)
        assert "已发起取消" in reply and bot.api.canceled == [TASK_ID, TASK_ID]

        reply, _, _ = await command(bot, "stopall", "/stopall")
        assert "已对 1 个任务发起取消" in reply and len(bot.api.canceled) == 3

        await ctx.run_tasks()
        assert not bot.active
        reply, _, _ = await command(bot, "tasks", "/tasks")
        assert "当前没有进行中的任务" in reply

    asyncio.run(run())


def test_stopall_stops_remaining_auto_subscriptions(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        app = FakeContext().application
        original = bot.api.get_task

        async def get_task(task_id):
            if bot._stop_gen == 0:  # 第一个订阅测速期间管理员发送 /stopall
                await command(bot, "stopall", "/stopall")
            return await original(task_id)

        bot.api.get_task = get_task
        await bot.run_auto(app, [-100], "🕘")
        assert bot.api.calls == ["3399 · 测速 · 自动测速"]  # IPLC 没有提交

    asyncio.run(run())


def test_status(tmp_path):
    async def run():
        bot = make_bot(tmp_path, schedule_spec="09:00")
        bot.active["x"] = ActiveTask("x", "3399", "🕘", None, [-100])
        reply, _, _ = await command(bot, "status", "/status")
        assert "进行中的任务：1 个" in reply and "今日测速：0 次" in reply and "每天 09:00，下次" in reply
        assert "在线 3/4" in reply

    asyncio.run(run())


def test_autotest_from_private_chat_posts_to_auto_chats(tmp_path):
    async def run():
        bot = make_bot(tmp_path, allowed_chat_ids={-100, -200})
        reply, _, ctx = await command(bot, "autotest", "/autotest")
        assert "结果发到 2 个群" in reply
        await ctx.run_tasks()
        assert {c for c, _, _, _ in ctx.bot.sent} >= {-100, -200}

    asyncio.run(run())


def test_settings_file_with_bad_values_is_ignored(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "cooldown_seconds": "abc", "schedule_spec": "* * * * *", "allowed_chat_ids": [-300]}), encoding="utf-8")
    bot = make_bot(tmp_path, schedule_spec="09:00")
    assert bot.cfg.cooldown_seconds == 0 and bot.schedule is None and bot.cfg.allowed_chat_ids == {-300}
    assert isinstance(bot, SpeedBot) and FakeUser(1).id == 1


# ---------------------------------------------------------------- 审查发现的问题的回归测试

def test_stopall_cancels_submissions_in_flight(tmp_path):
    async def run():
        for auto in (True, False):
            bot = make_bot(tmp_path / str(auto), backend_select=False, sort_select=False)
            submit = bot.api.submit_task

            async def submit_then_stop(*a, **kw):  # 提交请求还没返回时管理员发送 /stopall
                reply, _, _ = await command(bot, "stopall", "/stopall")
                assert "正在提交" in reply
                return await submit(*a, **kw)

            bot.api.submit_task = submit_then_stop
            app = FakeContext().application
            if auto:
                await bot.run_auto(app, [-100], "🕘")
                assert bot.api.calls == ["3399 · 测速 · 自动测速"]  # 剩下的订阅不再提交
            else:
                ctx = FakeContext(app=app)
                await member_submit(bot, ctx)
                await ctx.run_tasks()
            assert bot.api.canceled == [TASK_ID]

    asyncio.run(run())


def test_stopall_also_stops_round_waiting_for_lock(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        app = FakeContext().application
        async with bot._auto_lock:  # 管理员手动测速进行中，定时测速排队等待
            queued = asyncio.ensure_future(bot.run_auto(app, [-100], "🕘", wait_for_lock=True))
            await asyncio.sleep(0)
            await command(bot, "stopall", "/stopall")
        await queued
        assert bot.api.calls == []

    asyncio.run(run())


def test_cooldown_click_keeps_menu(tmp_path):
    async def run():
        bot = make_bot(tmp_path, daily_limit=0, cooldown_seconds=300, backend_select=False, max_tasks_per_chat=2)
        ctx = FakeContext()
        await member_submit(bot, ctx)
        await member_submit(bot, ctx, text="trojan://pw@b.com:443#B")  # 第二个菜单
        first, second = list(bot.selections)
        await click(bot, f"sel:{first}:o:0", user_id=1, ctx=ctx)
        q = await click(bot, f"sel:{second}:o:0", user_id=1, ctx=ctx)
        assert "测速太频繁" in q.answers[-1] and second in bot.selections
        await ctx.run_tasks()

    asyncio.run(run())


def test_removed_admin_cannot_finish_airport_menu(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        await command(bot, "admin", "/admin add 5")
        msg, ctx = await speed(bot, user_id=5)
        sid = next(iter(bot.selections))
        await command(bot, "admin", "/admin del 5")
        q = await click(bot, f"sel:{sid}:u:0", user_id=5, ctx=ctx)
        assert "已不是管理员" in q.answers[-1] and bot.api.submitted is None
        # 别的管理员点也不能替他提交
        q = await click(bot, f"sel:{sid}:u:0", user_id=ADMIN, ctx=ctx)
        assert "已不是管理员" in q.answers[-1] and bot.api.submitted is None

    asyncio.run(run())


def test_forum_topic_root_is_not_a_target(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        root = FakeMessage()
        root.from_user = SimpleNamespace(id=555, is_bot=False)
        root.forum_topic_created = SimpleNamespace(name="话题")
        reply, _, _ = await command(bot, "ban", "/ban", private=False, reply_to=root)
        assert "禁止测速的用户" in reply and not bot.cfg.banned_user_ids
        reply, _, _ = await command(bot, "admin", "/admin add", private=False, reply_to=root)
        assert "请提供用户 ID" in reply and not bot.cfg.extra_admin_ids

        upd, msg = update("/ban", ADMIN)
        msg.is_topic_message, msg.message_thread_id = True, 42
        msg.reply_to_message = FakeMessage()
        msg.reply_to_message.message_id = 42
        msg.reply_to_message.from_user = SimpleNamespace(id=556, is_bot=False)
        assert bot._target_user(msg, []) is None

    asyncio.run(run())


def test_removed_group_gets_no_auto_tests_even_with_auto_chat_ids(tmp_path):
    async def run():
        bot = make_bot(tmp_path, allowed_chat_ids={-100, -200}, auto_chat_ids={-100, -200})
        await command(bot, "group", "/group del -200")
        assert bot._auto_chats() == [-100]

    asyncio.run(run())


def test_airport_malformed_urls(tmp_path):
    async def run():
        bot = make_bot(tmp_path, subscriptions=[("坏的", "https://[abc/sub"), ("带 空格", "https://a.example/s")])
        reply, _, _ = await command(bot, "airport", "/airport")
        assert "坏的" in reply and "a.example" in reply
        reply, _, _ = await command(bot, "airport", "/airport add x https://[abc/sub")
        assert "格式不正确" in reply and len(bot.cfg.subscriptions) == 2
        reply, _, _ = await command(bot, "airport", "/airport del 带 空格")
        assert "已删除" in reply and [n for n, _ in bot.cfg.subscriptions] == ["坏的"]

    asyncio.run(run())


def test_ban_list_is_capped(tmp_path):
    async def run():
        bot = make_bot(tmp_path, banned_user_ids=set(range(1000, 1500)))
        reply, _, _ = await command(bot, "ban", "/ban")
        assert "还有 400 人" in reply and len(reply) < 4000

    asyncio.run(run())


def test_admin_menu_set_when_admin_first_messages_bot(tmp_path):
    from telegram.error import BadRequest

    async def run():
        bot = make_bot(tmp_path)
        ctx = FakeContext()
        fake = ctx.bot
        real_set = fake.set_my_commands

        async def chat_not_found(commands, scope=None, **kw):
            if scope is not None:
                raise BadRequest("Chat not found")
            await real_set(commands, scope=scope)

        fake.set_my_commands = chat_not_found
        await bot.set_commands(fake)  # 启动时管理员还没私聊过 bot
        assert ADMIN not in fake.commands
        fake.set_my_commands = real_set
        upd, _ = dm("/start")
        await bot.cmd_start(upd, ctx)
        assert "schedule" in fake.commands[ADMIN]

        # 已不是管理员的人，重启时去掉他的管理菜单
        await command(bot, "admin", "/admin add 5", ctx=ctx)
        bot.cfg.extra_admin_ids = set()
        bot.settings.set("extra_admin_ids", set())
        bot = restart(bot, tmp_path)
        await bot.set_commands(fake)
        assert 5 not in fake.commands and ADMIN in fake.commands

    asyncio.run(run())


def test_corrupt_settings_file_stops_startup(tmp_path):
    import pytest

    (tmp_path / "settings.json").write_text('{"allowed_chat_ids": [-100],\n}', encoding="utf-8")
    with pytest.raises(SystemExit, match="无法读取设置文件"):
        make_bot(tmp_path)
    (tmp_path / "settings.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SystemExit):
        make_bot(tmp_path)
