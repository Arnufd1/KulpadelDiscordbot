"""The displayed interval must remain the selected interval at checkout."""
from datetime import datetime, timedelta, timezone
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from test_web import portal, sign_in
from padelbot import booking, booking_manager, checkout, client, web_worker
from padelbot.config import settings


START = '2030-01-01T18:00:00Z'
SHORT = '2030-01-01T19:00:00Z'
LONG = '2030-01-01T19:30:00Z'


@pytest.fixture
def worker(portal, monkeypatch):
    app, _, store, (_, alice, _) = portal
    service = app.state.platform
    service.hub.register(alice)
    store.preferences(alice['id'], {'payment_mode': 'discord'})
    monkeypatch.setenv('PADELWEB_DB', str(store.path))
    monkeypatch.setenv('PADELWEB_USER_ID', alice['id'])
    monkeypatch.setenv('PADELWEB_JOB_ID', 'interval-review')
    monkeypatch.setattr(web_worker, 'initialise_key', lambda: None)
    monkeypatch.setattr(web_worker, 'platform_context', lambda: service.community)
    monkeypatch.setattr(settings, 'padelbot_rules_file', store.root / 'interval-rules.sqlite3')

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def me(self):
            return {'id': 42, 'balance': 20}

    monkeypatch.setattr(client, 'BackboneClient', Client)
    available = [booking.Slot(100, 200, START, end, None, {}) for end in (SHORT, LONG)]
    monkeypatch.setattr(booking, 'find_slots', lambda *a, **kw: available)
    monkeypatch.setattr(booking_manager, 'list_my_bookings', lambda *a, **kw: [])
    priced, reserved = [], []

    def price(client, slot, me):
        priced.append(slot)
        return Decimal('12') if slot.end_iso == LONG else Decimal('8')

    def reserve(client, slot, **kwargs):
        reserved.append(slot)
        return SimpleNamespace(paid=True, sale_id=12, booking_id=55, payment_url=None)

    monkeypatch.setattr(checkout, 'slot_price', price)
    monkeypatch.setattr(booking, 'book', reserve)
    return available, priced, reserved


@pytest.mark.parametrize('kind', ['quote', 'book'])
@pytest.mark.parametrize('end,expected,amount', [
    (SHORT, SHORT, 8), (LONG, LONG, 12),
    ('2030-01-01T20:30:00+01:00', LONG, 12),
])
def test_worker_uses_exact_selected_interval(worker, kind, end, expected, amount):
    available, priced, reserved = worker
    result = web_worker.execute(kind, {
        'product': 100, 'court': 200, 'target': START, 'end': end, 'amount': amount,
    })
    assert priced[0].end_iso == expected
    assert result['end'] == expected
    assert result['amount'] == str(amount)
    assert reserved == ([] if kind == 'quote' else [priced[0]])


@pytest.mark.parametrize('kind', ['quote', 'book'])
@pytest.mark.parametrize('end', [None, '2030-01-01T20:00:00Z', START,
                                '2030-01-01T17:00:00Z', '2030-01-01T19:30:00'])
def test_missing_ambiguous_or_invalid_end_cannot_quote_or_book(worker, kind, end):
    _, priced, reserved = worker
    payload = {'product': 100, 'court': 200, 'target': START, 'amount': 12}
    if end is not None:
        payload['end'] = end
    with pytest.raises(ValueError):
        web_worker.execute(kind, payload)
    assert priced == reserved == []


def test_unique_legacy_selection_remains_compatible(worker):
    available, priced, reserved = worker
    del available[0]
    result = web_worker.execute('quote', {
        'product': 100, 'court': 200, 'target': START, 'amount': 1,
    })
    assert result['end'] == LONG and priced == available and not reserved


@pytest.mark.parametrize('path', ['/api/quote', '/api/book'])
def test_api_preserves_exact_end_in_canonical_job(portal, path):
    _, client, store, (_, alice, _) = portal
    headers = sign_in(client, store, alice)
    payload = {'product': 100, 'court': 200, 'target': START, 'end': LONG, 'amount': 12}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200
    with store.conn() as connection:
        saved = connection.execute('SELECT payload FROM jobs WHERE id=?', (response.json()['job_id'],)).fetchone()
    assert json.loads(saved['payload']) == payload


@pytest.mark.parametrize('end', [START, '2030-01-01T17:00:00Z', '2030-01-01T19:30:00', 'bad'])
def test_api_rejects_invalid_end_before_enqueue(portal, end):
    _, client, store, (_, alice, _) = portal
    headers = sign_in(client, store, alice)
    response = client.post('/api/quote', json={
        'product': 100, 'court': 200, 'target': START, 'end': end, 'amount': 1,
    }, headers=headers)
    assert response.status_code == 400
    with store.conn() as connection:
        assert connection.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_cross_frontend_retry_keeps_one_booking_and_cannot_change_duration(portal):
    app, client, store, (_, alice, _) = portal
    headers = sign_in(client, store, alice)
    payload = {'product': 100, 'court': 200, 'target': START, 'end': LONG, 'amount': 12}
    first = client.post('/api/book', json=payload, headers=headers).json()['job_id']
    service = app.state.platform
    retry = service.enqueue(alice, 'book', {**payload, 'end': '2030-01-01T20:30:00+01:00'})
    assert first == retry
    with pytest.raises(ValueError, match='andere eindtijd'):
        service.enqueue(alice, 'book', {**payload, 'end': SHORT})
    with store.conn() as connection:
        assert connection.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize('action', ['quote', 'confirm'])
def test_draft_price_and_confirmation_keep_stored_interval(portal, action):
    app, client, store, (_, alice, _) = portal
    headers = sign_in(client, store, alice)
    app.state.platform.hub.register(alice)
    start = datetime.now(timezone.utc) + timedelta(days=2, hours=1)
    end = start + timedelta(minutes=90)
    from padelbot.drafts import DraftStore
    draft = DraftStore(app.state.community).create(alice['id'], 100, 200, start.isoformat(), end.isoformat())
    ident = draft['id']
    response = client.post('/api/drafts/' + ident + '/' + action,
                           json={'amount': 12} if action == 'confirm' else {}, headers=headers)
    assert response.status_code == 200, response.text
    with store.conn() as connection:
        queued = connection.execute('SELECT payload FROM jobs WHERE id=?', (response.json()['job_id'],)).fetchone()
    assert json.loads(queued['payload'])['end'] == end.isoformat()
