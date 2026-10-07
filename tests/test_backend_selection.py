import asyncio

import pytest

from bot.api import APIError
from test_menu import FakeApp, FakeContext, click, make_bot, member_submit


@pytest.mark.parametrize("flow", ["scheduled", "member", "auto_button"])
def test_automatic_submission_refreshes_and_uses_an_eligible_backend(tmp_path, flow):
    async def run():
        bot = make_bot(tmp_path, subscriptions=[("test", "trojan://pw@hk.com:443#HK")],
                       allowed_backends={"OLD", "NEW", "LOCKED", "UPGRADE", "PRIVATE"},
                       backend_select=flow == "auto_button", sort_select=False)
        calls = 0

        async def backends():
            nonlocal calls
            calls += 1
            if calls == 1:  # Snapshot shown during node loading may become stale.
                return [{"client_id": "OLD", "is_online": True},
                        {"client_id": "NEW", "is_online": True}]
            return [
                {"client_id": "OLD", "is_online": False},
                {"client_id": "NOT_ALLOWED", "is_online": True},
                {"client_id": "LOCKED", "is_online": True, "locked": True},
                {"client_id": "UPGRADE", "is_online": True, "upgrade_required": True},
                {"client_id": "PRIVATE", "is_online": True, "allow_public_access": False},
                {"is_online": True},
                {"client_id": "NEW", "is_online": True, "display_name": "新后端"},
            ]

        bot.api.list_backends = backends
        ctx = FakeContext()
        if flow == "scheduled":
            await bot.run_auto(ctx.application, [-100], "自动测速")
        else:
            await member_submit(bot, ctx)
            if flow == "auto_button":
                sid = next(iter(bot.selections))
                await click(bot, f"sel:{sid}:b:auto", user_id=1, ctx=ctx)
            await ctx.run_tasks()
        assert bot.api.slave_id == "NEW"
        assert len(bot.api.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["empty", "lookup_error"])
def test_no_fresh_backend_does_not_submit_and_refunds_reservations(tmp_path, failure):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False, cooldown_seconds=300)
        original = bot.api.list_backends
        calls = 0

        async def backends():
            nonlocal calls
            calls += 1
            if calls == 1:
                return await original()
            if failure == "lookup_error":
                raise APIError("后端列表暂时不可用", status=503)
            return []

        bot.api.list_backends = backends
        ctx = FakeContext()
        message = await member_submit(bot, ctx)
        await ctx.run_tasks()
        assert not bot.api.calls  # No POST with a missing ID or stale backend.
        assert bot.quota.used(1) == 0
        assert 1 not in bot.last_test
        assert not bot.running[-100]
        assert "提交失败" in message.replies[-1][2].edits[-1][0]

    asyncio.run(run())


def test_explicit_automatic_backend_is_preserved(tmp_path):
    async def run():
        bot = make_bot(tmp_path, auto_slave_id="DGCT")
        await bot.run_auto(FakeApp(), [-100], "自动测速")
        assert bot.api.slave_id == "DGCT"

    asyncio.run(run())
