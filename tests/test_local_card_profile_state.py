"""A local card is a payment instrument, not evidence of a KUL login."""
import json
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from test_web import portal, sign_in
from padelbot.platform_service import PlatformService
from padelbot.web_runtime import tenant_env


def test_local_card_does_not_fabricate_kul_verification_or_balance(portal):
    app, client, store, (_, alice, _) = portal
    vault = app.state.cards
    vault.key = AESGCM.generate_key(bit_length=256)
    card = vault.save(alice['id'], holder='Example', number='4111111111111111',
                      month=12, year=2035)
    environment = tenant_env(store, alice)
    for name, body in [('PADELBOT_TOKEN_FILE', '{}'),
                       ('PADELBOT_ANDROID_PROFILE_FILE', json.dumps({'enrollment_verified': True}))]:
        path = Path(environment[name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    sign_in(client, store, alice)
    data = client.get('/api/me').json()
    assert data['connected'] and data['device_ready']
    assert data['kul_verified'] is False
    assert data['payment']['local_card']['id'] == card['id']
    assert data['payment']['stored_card'] is True
    assert data['payment'].get('balance') is None
    assert not data['payment'].get('checked')

    PlatformService(store, cards=vault).remember_payment(
        alice['id'], {'balance': '8.00', 'direct_debit': False})
    data = client.get('/api/me').json()
    assert data['kul_verified'] is True
    assert data['payment']['balance'] == '8.00'
    assert data['payment']['local_card']['id'] == card['id']


def test_provider_card_flags_cannot_enable_local_automatic_payments(portal):
    app, client, store, (_, alice, _) = portal
    service = PlatformService(store, cards=app.state.cards)
    service.remember_payment(alice['id'], {'balance': '8.00', 'direct_debit': False,
                                         'stored_card': True, 'stored_method': True})
    headers = sign_in(client, store, alice)
    payment = client.get('/api/me').json()['payment']
    assert payment['stored_card'] is False
    assert payment['stored_method'] is False
    assert payment['local_card'] is None
    response = client.post('/api/preferences', json={'automatic_payment': True,
        'payment_mode': 'kul', 'max_amount': 12}, headers=headers)
    assert response.status_code == 400
    assert service.can_autobook(alice['id']) is False
