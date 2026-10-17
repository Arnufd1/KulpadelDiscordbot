"""Encrypted single-card storage. Used by the credit-card payment fallback
(when the user has no SEPA mandate). Card details are entered via CLI on
the Pi — NEVER through Discord.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet


@dataclass
class Card:
    holder: str
    number: str = field(repr=False)
    exp_month: int
    exp_year: int   # 4-digit
    cvv: str = field(repr=False)

    def masked(self) -> str:
        last4 = self.number[-4:] if len(self.number) >= 4 else "????"
        return f"**** **** **** {last4}  exp {self.exp_month:02d}/{self.exp_year}  ({self.holder})"


def validate_card(card: Card, *, allow_missing_cvv: bool = False) -> None:
    """Reject incomplete, mistyped or expired details without echoing them."""
    if not card.holder.strip():
        raise ValueError("Vul de naam van de kaarthouder in.")
    number = card.number
    if not number.isascii() or not number.isdigit() or not 12 <= len(number) <= 19:
        raise ValueError("Het kaartnummer heeft geen geldig formaat.")
    digits = [int(d) for d in number[::-1]]
    total = sum(d if i % 2 == 0 else (2 * d - 9 if d > 4 else 2 * d) for i, d in enumerate(digits))
    if total % 10:
        raise ValueError("Het kaartnummer bevat waarschijnlijk een typfout.")
    now = datetime.now(ZoneInfo("Europe/Brussels"))
    if not 1 <= card.exp_month <= 12 or not now.year <= card.exp_year <= now.year + 30:
        raise ValueError("De vervaldatum is ongeldig.")
    if (card.exp_year, card.exp_month) < (now.year, now.month):
        raise ValueError("Deze kaart is vervallen.")
    if allow_missing_cvv and card.cvv == '':
        return
    if not card.cvv.isascii() or not card.cvv.isdigit() or len(card.cvv) not in (3, 4):
        raise ValueError("De CVC moet uit 3 of 4 cijfers bestaan.")


class CardStore:
    def __init__(self, key_file: Path, blob_file: Path):
        self.key_file = key_file
        self.blob_file = blob_file

    def _fernet(self) -> Fernet:
        if not self.key_file.exists():
            raise FileNotFoundError(f"Master key missing: {self.key_file}. Run `padelbot init-key`.")
        return Fernet(self.key_file.read_bytes())

    def save(self, card: Card) -> None:
        validate_card(card)
        self.blob_file.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = self._fernet().encrypt(json.dumps(asdict(card)).encode())
        temporary = self.blob_file.with_suffix(self.blob_file.suffix + ".tmp")
        try:
            temporary.write_bytes(ciphertext)
            os.replace(temporary, self.blob_file)
        finally:
            if temporary.exists():
                temporary.unlink()
        try:
            os.chmod(self.blob_file, 0o600)
        except (OSError, NotImplementedError):
            pass

    def load(self) -> Card | None:
        if not self.blob_file.exists():
            return None
        plaintext = self._fernet().decrypt(self.blob_file.read_bytes())
        return Card(**json.loads(plaintext.decode()))

    def clear(self) -> None:
        if self.blob_file.exists():
            self.blob_file.unlink()
