import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from padelbot import discord_platform as ui
from padelbot import discord_ui as legacy
from padelbot.config import settings


OWNER = 42
USER = {'id': 'linked-profile'}


def interaction(user=OWNER):
    edits, replies, defers = [], [], []

    async def edit(**kwargs):
        edits.append(kwargs)

    async def send(content=None, **kwargs):
        replies.append({'content': content, **kwargs})
        return SimpleNamespace(flags=SimpleNamespace(ephemeral=False), jump_url='https://discord.com/channels/1/2/3')

    async def defer(**kwargs):
        defers.append(kwargs)

    return SimpleNamespace(user=SimpleNamespace(id=user),
        response=SimpleNamespace(edit_message=edit, send_message=send, defer=defer),
        followup=SimpleNamespace(send=send), edit_original_response=edit,
        edits=edits, replies=replies, defers=defers)


@pytest.fixture
def service(monkeypatch):
    from padelbot import identity
    monkeypatch.setattr(identity, 'remember_interaction', lambda *args: None)
    monkeypatch.setattr(ui.weather, 'get_forecast', lambda: None)
    monkeypatch.setattr(ui.weather, 'peek_forecast', lambda: None)
    monkeypatch.setattr(legacy, 'BackboneClient', lambda: pytest.fail('Platform UI must use the shared tenant queue'))
    writable = []

    def discord_user(discord_id):
        if discord_id != OWNER:
            raise ValueError('Koppel je eigen profiel.')
        return USER

    return SimpleNamespace(hub=object(), discord_user=discord_user,
        require_writable=lambda user_id: writable.append(user_id), writable=writable,
        store=SimpleNamespace(preferences=lambda _: {'payment_mode': 'discord'}))


def slots(day=None, count=3):
    base = (day or ui.today()).replace(hour=6)
    return [dict(product=product, court=product + 100, name=f'Padel {product}',
                 start=(base + timedelta(minutes=30 * index)).isoformat(),
                 end=(base + timedelta(minutes=30 * index + 90)).isoformat(), price=None)
            for index in range(count) for product in (1, 2)]


def overview(day=None, count=3, end=None):
    day = day or ui.today()
    return ui.availability_overview(slots(day, count), day, end or day)


@pytest.mark.parametrize('first,last,fragment', [(0, 5, '0-5'), (2, 2, '2-2'),
    (0, 6, '0-6'), (0, 7, '0-6'), (15, 20, '0-6')])
def test_dashboard_link_carries_range_and_falls_back_to_the_upcoming_week(first, last, fragment):
    base = ui.today()
    snapshot = overview(base + timedelta(days=first), end=base + timedelta(days=last))
    assert ui.availability_dashboard_url(snapshot) == settings.padelbot_public_url.rstrip('/') + '/#slots/' + fragment


def test_dashboard_buttons_keep_the_range_when_a_day_and_time_are_selected(service):
    async def run():
        base = ui.today()
        snapshot = overview(end=base + timedelta(days=5))
        ranged = ui.SlotsView(service, USER, OWNER, snapshot)
        day = ui.DaySlotsView(service, USER, OWNER, snapshot, base, parent=snapshot)
        chosen = day.replace(selected_time=day.time_select.options[0].value)
        for view in [ranged, day, chosen]:
            button = next(child for child in view.children if getattr(child, 'label', '') == 'Dashboard')
            assert button.url.endswith('/#slots/0-5')
        direct = ui.DaySlotsView(service, USER, OWNER, overview(), base)
        assert next(child for child in direct.children if getattr(child, 'label', '') == 'Dashboard').url.endswith('/#slots/0-0')

    asyncio.run(run())


def test_range_command_visibly_lists_all_free_times_and_courts_plus_empty_days(service, monkeypatch):
    calls = []

    async def queued(svc, user, kind, payload):
        calls.append((svc, user, kind, payload))
        return {'slots': slots(count=3)}

    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        it = interaction()
        await ui.command(it, service, 'slots', 0, 5)
        assert calls == [(service, USER, 'slots', {'start': 0, 'end': 5})]
        sent = it.edits[-1]
        assert sent['content'] is None
        assert isinstance(sent['view'], ui.SlotsView)
        assert len(sent['view'].select.options) == 6
        assert all(f'**{clock}**' in sent['embed'].description for clock in ('06:00–07:30', '06:30–08:00', '07:00–08:30'))
        assert 'Padel 1, Padel 2' in sent['embed'].description
        assert sent['embed'].description.count('Geen beschikbare padelvelden.') == 5
        assert any(getattr(item, 'label', '') == 'Vernieuwen' for item in sent['view'].children)
        assert it.defers == [{'thinking': True, 'ephemeral': True}]

    asyncio.run(run())


def test_single_day_command_opens_the_time_and_court_panel_directly(service, monkeypatch):
    async def queued(svc, user, kind, payload):
        assert payload == {'start': 2, 'end': 2}
        return {'slots': slots(ui.today() + timedelta(days=2))}

    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        it = interaction()
        await ui.command(it, service, 'slots', 2, 2)
        view = it.edits[-1]['view']
        assert isinstance(view, ui.DaySlotsView)
        assert [option.label for option in view.time_select.options] == ['06:00–07:30', '06:30–08:00', '07:00–08:30']
        assert 'Padel 1, Padel 2' in it.edits[-1]['embed'].description
        assert not any(getattr(item, 'label', '') == 'Terug naar alle dagen' for item in view.children)

    asyncio.run(run())


def test_equal_start_different_end_keeps_exact_interval_through_quote_and_booking(service, monkeypatch):
    rows = slots(count=1)
    longer = {**rows[0], 'end': (ui.today().replace(hour=9)).isoformat(), 'price': 14}
    rows.extend([dict(rows[0]), longer])
    calls = []

    async def queued(svc, user, kind, payload):
        calls.append((kind, payload))
        if kind == 'quote':
            return {'amount': '14.00', 'end': longer['end']}
        return {'message': 'Geboekt.', 'paid': False}
    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        snapshot = ui.availability_overview(rows, ui.today(), ui.today())
        view = ui.DaySlotsView(service, USER, OWNER, snapshot, ui.today())
        assert [option.label for option in view.time_select.options] == ['06:00–07:30', '06:00–09:00']
        assert snapshot.pages[0][0].count('**06:00–07:30**') == 1
        assert snapshot.pages[0][0].count('**06:00–09:00**') == 1
        it = interaction()
        view.time_select._values = [view.time_select.options[-1].value]
        await view.choose_time(it)
        timed = it.edits[-1]['view']
        assert [option.label for option in timed.court_select.options] == ['Padel 1']
        timed.court_select._values = [timed.court_select.options[0].value]
        await timed.choose_court(it)
        confirmation = it.edits[-1]['view']
        assert confirmation.slot['end'] == longer['end'] and confirmation.slot['price'] == 14
        await confirmation.confirm(it)
        assert calls == [
            ('quote', {'target': longer['start'], 'end': longer['end'], 'product': 1, 'court': 101, 'amount': 1}),
            ('book', {'target': longer['start'], 'end': longer['end'], 'product': 1, 'court': 101, 'amount': '14.00'})]
    asyncio.run(run())


def test_shared_public_formatter_and_private_controls_deduplicate_and_naturally_sort_courts(service):
    first = slots(count=1)[0]
    rows = [{**first, 'product': number, 'court': 100 + number, 'name': f'Veld {number}'}
            for number in (10, 2, 1)]
    rows += [dict(rows[0])]
    async def run():
        snapshot = ui.availability_overview(rows, ui.today(), ui.today())
        view = ui.DaySlotsView(service, USER, OWNER, snapshot, ui.today())
        assert len(view.time_select.options) == 1
        it = interaction()
        view.time_select._values = [view.time_select.options[0].value]
        await view.choose_time(it)
        chosen = it.edits[-1]['view']
        assert [option.label for option in chosen.court_select.options] == ['Veld 1', 'Veld 2', 'Veld 10']
        await chosen.share(it)
        published = it.replies[-1]
        assert published['ephemeral'] is False
        assert published['embed'].description == chosen.embed().description
        assert published['embed'].description.count('**06:00–07:30**') == 1
        assert 'Veld 1, Veld 2, Veld 10' in published['embed'].description
    asyncio.run(run())


def test_more_than_25_courts_are_reachable_without_changing_the_selected_interval(service, monkeypatch):
    first = slots(count=1)[0]
    rows = [{**first, 'product': number, 'court': 100 + number, 'name': f'Veld {number}'}
            for number in reversed(range(1, 31))]
    calls = []
    async def queued(svc, user, kind, payload):
        calls.append((kind, payload))
        return {'amount': '8.00', 'end': payload['end']}
    monkeypatch.setattr(ui, 'job', queued)
    async def run():
        snapshot = ui.availability_overview(rows, ui.today(), ui.today())
        view = ui.DaySlotsView(service, USER, OWNER, snapshot, ui.today())
        chosen = view.replace(selected_time=view.time_select.options[0].value)
        assert len(chosen.court_select.options) == 25
        assert chosen.court_select.options[-1].label == 'Veld 25'
        it = interaction()
        await chosen.next_courts(it)
        last_page = it.edits[-1]['view']
        assert last_page.selected_time == chosen.selected_time
        assert [option.label for option in last_page.court_select.options] == [f'Veld {number}' for number in range(26, 31)]
        assert len(last_page.to_components()) <= 5
        last_page.court_select._values = [last_page.court_select.options[-1].value]
        await last_page.choose_court(it)
        assert calls == [('quote', {'target': first['start'], 'end': first['end'], 'product': 30, 'court': 130, 'amount': 1})]
        await it.edits[-1]['view'].back(it)
        restored = it.edits[-1]['view']
        assert restored.court_page == 1 and restored.selected_time == chosen.selected_time
    asyncio.run(run())


def test_large_requested_range_keeps_every_date_but_only_fetches_bookable_horizon(service, monkeypatch):
    calls = []

    async def queued(svc, user, kind, payload):
        calls.append((kind, payload))
        return {'slots': []}

    monkeypatch.setattr(ui, 'job', queued)
    monkeypatch.setattr(settings, 'padel_opens_days_ahead', 7)

    async def run():
        base = ui.today()
        result = await ui.load_availability(service, USER, base, base + timedelta(days=365))
        assert calls == [('slots', {'start': 0, 'end': 7})]
        days = {day.date() for _, page_days in result.pages for day in page_days}
        assert len(days) == 366
        assert base.date() in days and (base + timedelta(days=365)).date() in days
        assert all(len(body) <= 3900 and len(page_days) <= 25 for body, page_days in result.pages)
        assert any('Nog niet boekbaar' in body for body, _ in result.pages)
        view = ui.SlotsView(service, USER, OWNER, result)
        it = interaction()
        await view.next(it)
        second = it.edits[-1]
        assert second['embed'].description == result.pages[1][0]
        assert len(second['view'].select.options) <= 25

    asyncio.run(run())


def test_future_only_range_never_enqueues_a_kul_request(service, monkeypatch):
    async def forbidden(*args):
        pytest.fail('Unopened dates must not query the KUL API')

    monkeypatch.setattr(ui, 'job', forbidden)

    async def run():
        start = ui.today() + timedelta(days=30)
        result = await ui.load_availability(service, USER, start, start + timedelta(days=2))
        assert len(result.pages[0][1]) == 3
        assert result.pages[0][0].count('Nog niet boekbaar') == 3

    asyncio.run(run())


def test_time_pages_and_court_choice_reach_last_slot_and_only_quote_before_confirmation(service, monkeypatch):
    calls = []

    async def queued(svc, user, kind, payload):
        calls.append((svc, user, kind, payload))
        if kind == 'quote':
            return {'amount': '8.00'}
        assert kind == 'book'
        return {'message': 'Geboekt.', 'paid': False}

    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        snapshot = overview(count=30)
        view = ui.DaySlotsView(service, USER, OWNER, snapshot, ui.today())
        it = interaction()
        assert len(view.time_select.options) == 25
        await view.next_times(it)
        next_page = it.edits[-1]['view']
        assert len(next_page.time_select.options) == 5
        next_page.time_select._values = [next_page.time_select.options[-1].value]
        await next_page.time_select.callback(it)
        timed = it.edits[-1]['view']
        assert [option.label for option in timed.court_select.options] == ['Padel 1', 'Padel 2']
        timed.court_select._values = [timed.court_select.options[-1].value]
        await asyncio.gather(timed.choose_court(it), timed.choose_court(it))
        chosen = slots(count=30)[-1]
        assert calls == [(service, USER, 'quote', {
            'target': chosen['start'], 'end': chosen['end'], 'product': 2, 'court': 102, 'amount': 1})]
        confirmation = it.edits[-1]['view']
        assert isinstance(confirmation, ui.ConfirmView)
        assert it.edits[-1]['embed'] is None
        await asyncio.gather(confirmation.confirm(it), confirmation.confirm(it))
        assert calls[-1] == (service, USER, 'book', {
            'target': chosen['start'], 'end': chosen['end'], 'product': 2, 'court': 102, 'amount': '8.00'})
        assert [kind for _, _, kind, _ in calls] == ['quote', 'book']

    asyncio.run(run())


def test_day_navigation_and_refresh_fetch_the_requested_dates_through_shared_jobs(service, monkeypatch):
    calls = []

    async def queued(svc, user, kind, payload):
        calls.append((svc, user, kind, payload))
        return {'slots': []}

    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        target = ui.today() + timedelta(days=2)
        parent = overview(end=ui.today() + timedelta(days=5))
        view = ui.DaySlotsView(service, USER, OWNER, parent, target, parent=parent)
        it = interaction()
        await view.next_day(it)
        following = it.edits[-1]['view']
        assert following.target == target + timedelta(days=1)
        assert 'Geen beschikbare padelvelden.' in following.embed().description
        await following.refresh(it)
        refreshed = it.edits[-1]['view']
        await refreshed.back(it)
        assert it.edits[-1]['view'].overview is parent
        assert calls == [(service, USER, 'slots', {'start': 3, 'end': 3})] * 2

    asyncio.run(run())


def test_confirmation_back_restores_live_day_controls_and_range_without_new_jobs(service, monkeypatch):
    calls = []

    async def queued(svc, user, kind, payload):
        calls.append(kind)
        assert kind == 'quote'
        return {'amount': '8.00'}

    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        snapshot = overview(end=ui.today() + timedelta(days=5))
        range_view = ui.SlotsView(service, USER, OWNER, snapshot)
        it = interaction()
        range_view.select._values = [ui.today().date().isoformat()]
        await range_view.choose(it)
        day = it.edits[-1]['view']
        assert day.share_overview.checked_at == snapshot.checked_at
        day.time_select._values = [day.time_select.options[0].value]
        await day.choose_time(it)
        chosen = it.edits[-1]['view']
        chosen.court_select._values = [chosen.court_select.options[0].value]
        await chosen.choose_court(it)
        confirmation = it.edits[-1]['view']
        assert chosen.is_finished()
        await confirmation.back(it)
        restored = it.edits[-1]['view']
        assert isinstance(restored, ui.DaySlotsView) and not restored.is_finished()
        assert restored.share_overview is day.share_overview
        assert restored.selected_time == chosen.selected_time
        assert restored.parent is snapshot
        await restored.back(it)
        assert it.edits[-1]['view'].overview is snapshot
        assert calls == ['quote']

    asyncio.run(run())


def test_failed_refresh_preserves_snapshot_and_allows_retry(service, monkeypatch):
    async def queued(*args):
        raise ValueError('Niet beschikbaar')

    monkeypatch.setattr(ui, 'job', queued)

    async def run():
        view = ui.SlotsView(service, USER, OWNER, overview())
        it = interaction()
        await view.refresh(it)
        assert not view.busy and not view.is_finished()
        assert not it.edits
        assert it.replies[-1]['ephemeral']
        assert 'Vernieuwen' in it.replies[-1]['content']

    asyncio.run(run())


def test_foreign_identity_cannot_use_or_share_private_availability(service):
    async def run():
        view = ui.SlotsView(service, USER, OWNER, overview())
        it = interaction(99)
        assert not await view.interaction_check(it)
        await view.share(it)
        assert len(it.replies) == 2
        assert all(reply.get('ephemeral') is True for reply in it.replies)
        assert not service.writable

    asyncio.run(run())


def test_relinked_identity_cannot_reuse_an_old_price_confirmation(service, monkeypatch):
    async def forbidden(*args):
        pytest.fail('A stale profile confirmation must not submit any job')

    monkeypatch.setattr(ui, 'job', forbidden)
    service.discord_user = lambda _: {'id': 'different-profile'}

    async def run():
        view = ui.ConfirmView(service, USER, OWNER, slots()[0], {'amount': '8.00'})
        it = interaction()
        if await view.interaction_check(it):
            await view.confirm(it)
        assert view.user is USER
        assert len(it.replies) == 1 and it.replies[0]['ephemeral']
        assert 'profielkoppeling is gewijzigd' in it.replies[0]['content']

    asyncio.run(run())


def test_restricted_profile_cannot_publish_availability(service):
    def denied(user_id):
        raise ValueError('Dit profiel is beperkt.')

    service.require_writable = denied

    async def run():
        view = ui.SlotsView(service, USER, OWNER, overview())
        it = interaction()
        await view.share(it)
        assert it.replies == [{'content': 'Dit profiel is beperkt.', 'ephemeral': True}]

    asyncio.run(run())


def test_sharing_latch_survives_day_and_time_navigation_and_public_controls_are_read_only(service):
    async def run():
        snapshot = overview(end=ui.today() + timedelta(days=60))
        ranged = ui.SlotsView(service, USER, OWNER, snapshot)
        it = interaction()
        ranged.select._values = [ui.today().date().isoformat()]
        await ranged.choose(it)
        day = it.edits[-1]['view']
        await day.share(it)
        assert it.replies[-1]['ephemeral'] is False
        day.time_select._values = [day.time_select.options[0].value]
        await day.choose_time(it)
        selected = it.edits[-1]['view']
        assert selected.share_overview is day.share_overview
        await selected.share(it)
        assert 'al gedeeld' in it.replies[-1]['content']
        await selected.back(it)
        returned = it.edits[-1]['view']
        returned.select._values = [ui.today().date().isoformat()]
        await returned.choose(it)
        assert it.edits[-1]['view'].share_overview is day.share_overview

        await returned.share(it)
        published = it.replies[-1]
        assert published['ephemeral'] is False
        assert published['file'].filename == 'padel-beschikbaarheid.txt'
        assert not published['allowed_mentions'].everyone
        public = published['view']
        assert await public.interaction_check(interaction(99))
        assert [item.label for item in public.children] == ['Vorige pagina', 'Volgende pagina']
        await public.next_page(it)
        assert it.edits[-1]['view'].public
        await returned.share(it)
        assert sum(reply.get('ephemeral') is False for reply in it.replies) == 2
        assert 'al gedeeld' in it.replies[-1]['content']

    asyncio.run(run())
