from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from cryptography.fernet import Fernet
import pytest

from padelbot.card_store import Card, CardStore, validate_card
from padelbot.cli import cli
from padelbot.config import settings


CARD = Card("Test", "4242424242424242", 12, datetime.now(ZoneInfo("Europe/Brussels")).year + 3, "123")


def test_encrypted_card_roundtrip_contains_no_plaintext_secrets(tmp_path):
    key, blob = tmp_path / "master.key", tmp_path / "card.enc"
    key.write_bytes(Fernet.generate_key())
    store = CardStore(key, blob)
    store.save(CARD)
    assert store.load() == CARD
    assert CARD.number.encode() not in blob.read_bytes()
    assert not blob.with_suffix(".enc.tmp").exists()


@pytest.mark.parametrize("fields", [
    {"number": "4242424242424243"}, {"number": "not-a-card"}, {"holder": ""},
    {"exp_month": 0}, {"exp_month": 13}, {"exp_year": 2000},
    {"cvv": "12"}, {"cvv": "secret-cvc"},
])
def test_invalid_card_is_rejected_without_echoing_values(tmp_path, fields):
    card = replace(CARD, **fields)
    store = CardStore(tmp_path / "master.key", tmp_path / "card.enc")
    with pytest.raises(ValueError) as error:
        store.save(card)
    assert card.number not in str(error.value)
    assert card.cvv not in str(error.value)
    assert not store.blob_file.exists()


def test_cli_hides_card_number_and_cvc_and_saves_encrypted(tmp_path, monkeypatch):
    key, blob = tmp_path / "master.key", tmp_path / "card.enc"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings, "padelbot_key_file", key)
    monkeypatch.setattr(settings, "padelbot_card_file", blob)
    result = CliRunner().invoke(cli, ["set-card"], input=f"{CARD.holder}\n{CARD.number}\n{CARD.exp_month}\n{CARD.exp_year}\n{CARD.cvv}\n")
    assert result.exit_code == 0, result.output
    assert CARD.number not in result.output and CARD.cvv not in result.output
    assert CardStore(key, blob).load() == CARD
