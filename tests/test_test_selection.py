import asyncio

import pytest
from telegram.error import TelegramError

from bot.api import APIError
from bot.formatter import format_stats
from test_menu import ADMIN, TASK_ID, FakeContext, FakeMessage, FakeQuery, YieldingQuery, buttons, click, make_bot, update


async def open_menu(bot, ctx, member=False):
    if member:
        upd, msg = update('trojan://pw@other.com:443#节点', user_id=1, chat_id=1, chat_type='private')
        await bot.on_private_text(upd, ctx)
    else:
        upd, msg = update('/speed 3399', user_id=ADMIN)
        ctx.args = ['3399']
        await bot.cmd_speed(upd, ctx)
    return msg.replies[-1][2]


@pytest.mark.parametrize('member', [False, True])
def test_manual_submission_waits_for_test_selection_even_without_other_pickers(tmp_path, member):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx, member)
        assert bot.api.submitted is None
        assert '选择测试内容' in status.edits[-1][0]
        sid = next(iter(bot.selections))
        await click(bot, f'sel:{sid}:go', user_id=1 if member else ADMIN, ctx=ctx)
        assert [m['Type'] for m in bot.api.submitted[2]] == [
            'TEST_PING_RTT', 'SPEED_AVERAGE', 'SPEED_MAX', 'SPEED_PER_SECOND']
        await ctx.run_tasks()
    asyncio.run(run())


def test_topology_only_skips_speed_sort_and_exports_topology(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        exports = []

        async def export(task_id, view, sort=None):
            exports.append((view, sort))
            return b'img'

        bot.api.export_image = export
        await click(bot, f'sel:{sid}:clear', ctx=ctx)
        await click(bot, f'sel:{sid}:t:geo', ctx=ctx)
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert bot.api.submitted is not None
        assert bot.api.submitted[2] == [
            {'Type': 'GEOIP_INBOUND', 'Params': ''}, {'Type': 'GEOIP_OUTBOUND', 'Params': ''}]
        assert '出入口拓扑' in bot.api.submitted[0]
        await ctx.run_tasks()
        assert exports == [('topologyview', None)]
        assert len(status.photos) == 1
    asyncio.run(run())


def test_empty_selection_and_other_users_cannot_submit(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        await open_menu(bot, ctx, member=True)
        sid = next(iter(bot.selections))
        q = await click(bot, f'sel:{sid}:clear', user_id=2, ctx=ctx)
        assert '只有发起人' in q.answers[0]
        await click(bot, f'sel:{sid}:clear', user_id=1, ctx=ctx)
        q = await click(bot, f'sel:{sid}:go', user_id=1, ctx=ctx)
        assert '至少选择' in q.answers[0]
        assert bot.api.submitted is None and bot.quota.used(1) == 0
        assert sid in bot.selections
        await click(bot, f'sel:{sid}:t:udp', user_id=1, ctx=ctx)
        await click(bot, f'sel:{sid}:go', user_id=1, ctx=ctx)
        assert bot.api.submitted[2] == [{'Type': 'UDP_TYPE', 'Params': ''}]
        await ctx.run_tasks()
    asyncio.run(run())


def test_streaming_scripts_use_api_ids_and_persist_across_pages(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))

        async def scripts():
            return [{'id': 'IP_IPSB', 'name': 'IPSB', 'type': 'ip'}] + [
                {'id': f'media{i}', 'name': f'Media {i}', 'type': 'media'} for i in range(14)]

        bot.api.list_scripts = scripts
        await click(bot, f'sel:{sid}:clear', ctx=ctx)
        await click(bot, f'sel:{sid}:scripts', ctx=ctx)
        markup = status.edits[-1][1]
        assert 'IPSB' not in ' '.join(buttons(markup))
        assert any('Media 0' in text for text in buttons(markup))
        await click(bot, f'sel:{sid}:s:0', ctx=ctx)
        next_button = next(b for row in status.edits[-1][1].inline_keyboard for b in row if b.text == '下一页')
        await click(bot, next_button.callback_data, ctx=ctx)
        last_button = next(b for row in status.edits[-1][1].inline_keyboard for b in row if 'Media 13' in b.text)
        await click(bot, last_button.callback_data, ctx=ctx)
        await click(bot, f'sel:{sid}:back', ctx=ctx)
        assert 'Media 0' in status.edits[-1][0] and 'Media 13' in status.edits[-1][0]
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert bot.api.submitted[2] == [
            {'Type': 'TEST_SCRIPT', 'Params': 'INTERNAL::media0'},
            {'Type': 'TEST_SCRIPT', 'Params': 'INTERNAL::media13'}]
        # Global scripts have no source in this API; the server resolves INTERNAL:: references.
        assert bot.api.configs['Scripts'] == []
        await ctx.run_tasks()
    asyncio.run(run())


def test_script_lookup_failure_allows_retry_and_other_tests(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        calls = 0

        async def scripts():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise APIError('暂时不可用')
            return [{'id': 'Netflix', 'name': 'Netflix', 'type': 'media'}]

        bot.api.list_scripts = scripts
        await click(bot, f'sel:{sid}:scripts', ctx=ctx)
        assert '获取流媒体脚本失败' in status.edits[-1][0]
        await click(bot, f'sel:{sid}:back', ctx=ctx)
        await click(bot, f'sel:{sid}:scripts', ctx=ctx)
        assert any('Netflix' in b for b in buttons(status.edits[-1][1]))
        await click(bot, f'sel:{sid}:back', ctx=ctx)
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert all(m['Type'] != 'TEST_SCRIPT' for m in bot.api.submitted[2])
        await ctx.run_tasks()
    asyncio.run(run())


def test_cancel_while_loading_scripts_does_not_reopen_menu(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        started, finish = asyncio.Event(), asyncio.Event()

        async def scripts():
            started.set()
            await finish.wait()
            return []

        bot.api.list_scripts = scripts
        loading = asyncio.create_task(click(bot, f'sel:{sid}:scripts', ctx=ctx))
        await asyncio.wait_for(started.wait(), 1)
        await click(bot, f'sel:{sid}:x', ctx=ctx)
        finish.set()
        await loading
        assert sid not in bot.selections and '已终止' in status.edits[-1][0]
    asyncio.run(run())


def test_double_click_confirmation_submits_once(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False, max_tasks_per_chat=5)
        ctx = FakeContext()
        await open_menu(bot, ctx, member=True)
        sid = next(iter(bot.selections))
        await asyncio.gather(*(bot._on_select(YieldingQuery(f'sel:{sid}:go', 1), ctx) for _ in range(2)))
        assert len(bot.api.calls) == 1 and bot.quota.used(1) == 1
        await ctx.run_tasks()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['go', 'x'])
def test_delayed_toggle_reply_cannot_replace_submitted_or_canceled_status(tmp_path, action):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        started, finish = asyncio.Event(), asyncio.Event()
        query = FakeQuery(f'sel:{sid}:t:geo', ADMIN)

        async def delayed_answer(*args, **kwargs):
            started.set()
            await finish.wait()

        query.answer = delayed_answer
        toggling = asyncio.create_task(bot._on_select(query, ctx))
        await asyncio.wait_for(started.wait(), 1)
        await click(bot, f'sel:{sid}:{action}', ctx=ctx)
        final_status = status.edits[-1]
        finish.set()
        await toggling
        assert status.edits[-1] == final_status
        await ctx.run_tasks()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['go', 'x'])
def test_in_flight_menu_edit_finishes_before_final_status(tmp_path, action):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        started, finish = asyncio.Event(), asyncio.Event()
        original_edit = status.edit_text

        async def delayed_edit(text, **kwargs):
            if '选择测试内容' in text:
                started.set()
                await finish.wait()
            await original_edit(text, **kwargs)

        status.edit_text = delayed_edit
        toggling = asyncio.create_task(click(bot, f'sel:{sid}:t:geo', ctx=ctx))
        await asyncio.wait_for(started.wait(), 1)
        confirming = asyncio.create_task(click(bot, f'sel:{sid}:{action}', ctx=ctx))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(toggling, confirming)
        assert '选择测试内容' not in status.edits[-1][0]
        assert status.edits[-1][1] is None
        await ctx.run_tasks()
    asyncio.run(run())


def test_mixed_manual_task_exports_both_selected_views(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        exports = []

        async def export(task_id, view, sort=None):
            exports.append((view, sort))
            return b'img'

        bot.api.export_image = export
        await click(bot, f'sel:{sid}:t:geo', ctx=ctx)
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert [m['Type'] for m in bot.api.submitted[2]] == [
            'TEST_PING_RTT', 'SPEED_AVERAGE', 'SPEED_MAX', 'SPEED_PER_SECOND',
            'GEOIP_INBOUND', 'GEOIP_OUTBOUND']
        await ctx.run_tasks()
        assert exports == [('normalview', 'avg_speed_desc'), ('topologyview', None)]
        assert len(status.photos) == 2
    asyncio.run(run())


def test_latency_selection_hides_speed_sort_and_ignores_stale_sort_button(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        await click(bot, f'sel:{sid}:t:speed', ctx=ctx)
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert not any('平均速度' in b for b in buttons(status.edits[-1][1]))
        await click(bot, f'sel:{sid}:o:3', ctx=ctx)
        assert bot.api.submitted is None
        rtt = next(b for row in status.edits[-1][1].inline_keyboard for b in row if 'RTT' in b.text)
        await click(bot, rtt.callback_data, ctx=ctx)
        assert bot.api.submitted[2] == [{'Type': 'TEST_PING_RTT', 'Params': ''}]
        await ctx.run_tasks()
        assert bot.api.sort == 'rtt_asc'
    asyncio.run(run())


@pytest.mark.parametrize(('option', 'matrix'), [
    ('conn', 'TEST_PING_CONN'), ('loss', 'TEST_PING_PACKET_LOSS'),
    ('http', 'TEST_HTTP_CODE'), ('hijack', 'TEST_HIJACK_DETECTION'),
])
def test_other_items_can_run_without_speed(tmp_path, option, matrix):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        await click(bot, f'sel:{sid}:clear', ctx=ctx)
        await click(bot, f'sel:{sid}:t:{option}', ctx=ctx)
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert bot.api.submitted[2] == [{'Type': matrix, 'Params': ''}]
        await ctx.run_tasks()
    asyncio.run(run())


def test_empty_script_catalog_can_return_to_defaults(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        status = await open_menu(bot, ctx)
        sid = next(iter(bot.selections))

        async def scripts():
            return []

        bot.api.list_scripts = scripts
        await click(bot, f'sel:{sid}:scripts', ctx=ctx)
        assert '暂无可用' in status.edits[-1][0]
        await click(bot, f'sel:{sid}:s:0', ctx=ctx)
        await click(bot, f'sel:{sid}:back', ctx=ctx)
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert len(bot.api.submitted[2]) == 4
        await ctx.run_tasks()
    asyncio.run(run())


def test_manual_selection_does_not_change_automatic_defaults(tmp_path):
    async def run():
        bot = make_bot(tmp_path, backend_select=False, sort_select=False)
        ctx = FakeContext()
        await open_menu(bot, ctx)
        sid = next(iter(bot.selections))
        await click(bot, f'sel:{sid}:clear', ctx=ctx)
        await click(bot, f'sel:{sid}:t:geo', ctx=ctx)
        await bot.run_auto(ctx.application, [-100], '自动测速')
        assert [m['Type'] for m in bot.api.submitted[2]] == [
            'TEST_PING_RTT', 'SPEED_AVERAGE', 'SPEED_MAX', 'SPEED_PER_SECOND']
        assert bot.api.configs['Scripts'] == []
        await click(bot, f'sel:{sid}:go', ctx=ctx)
        assert len(bot.api.submitted[2]) == 2
        await ctx.run_tasks()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, 'export', 'send'])
def test_result_retrieval_exports_both_views_and_preserves_partial_failures(tmp_path, failure):
    async def run():
        bot = make_bot(tmp_path)
        status = FakeMessage()
        exports = []

        async def result(task_id):
            return {'result': {'Results': [{'ProxyInfo': {'Name': 'HK'}, 'Matrices': [
                {'Type': 'TEST_PING_RTT', 'Payload': {'Value': 12}},
                {'Type': 'GEOIP_INBOUND', 'Payload': {}}, {'Type': 'GEOIP_OUTBOUND', 'Payload': {}}]}]}}

        async def export(task_id, view, sort=None):
            exports.append(view)
            if failure == 'export' and view == 'topologyview':
                raise APIError('export failed')
            return b'img'

        original_photo = status.reply_photo

        async def photo(*args, **kwargs):
            if status.photos:
                raise TelegramError('send failed')
            return await original_photo(*args, **kwargs)

        bot.api.get_result, bot.api.export_image = result, export
        if failure == 'send':
            status.reply_photo = photo
        post = await bot._send_result(status, TASK_ID, None, '结果')
        assert exports == ['normalview', 'topologyview']
        assert len(status.photos) == (2 if failure is None else 1)
        if failure == 'send':
            assert post.targets == []  # Preserve task ID when a view could not be delivered.
        else:
            assert post.targets == [status]
        if failure == 'export':
            assert status.replies  # Text fallback for the missing view.
    asyncio.run(run())


def test_topology_without_latency_does_not_claim_all_nodes_dead():
    stats = format_stats([{'Matrices': [{'Type': 'GEOIP_OUTBOUND', 'Payload': {}}]}])
    assert '可用 0/1' not in stats and '1' in stats


def test_http_only_result_survives_image_export_failure(tmp_path):
    async def run():
        bot = make_bot(tmp_path)
        status = FakeMessage()

        async def result(task_id):
            return {'result': {'Results': [{'ProxyInfo': {'Name': 'HK'}, 'Matrices': [
                {'Type': 'TEST_HTTP_CODE', 'Payload': {'Value': 204}}]}]}}

        async def export(*args):
            raise APIError('export failed')

        bot.api.get_result, bot.api.export_image = result, export
        post = await bot._send_result(status, TASK_ID, None, 'HTTP测试')
        assert post.targets == [status]
        assert 'HTTP 204' in status.replies[-1][0]
    asyncio.run(run())
