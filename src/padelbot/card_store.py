"""Encrypted single-card storage. Used by the credit-card payment fallback
(when the user has no SEPA mandate). Card details are entered via CLI on
the Pi — NEVER through Discord.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

from cryptography.fernet import Fernet


@dataclass
class Card:
    holder: str
    number: str
    exp_month: int
    exp_year: int   # 4-digit
    cvv: str

    def masked(self) -> str:
        last4 = self.number[-4:] if len(self.number) >= 4 else "????"
        return f"**** **** **** {last4}  exp {self.exp_month:02d}/{self.exp_year}  ({self.holder})"


class CardStore:
    def __init__(self, key_file: Path, blob_file: Path):
        self.key_file = key_file
        self.blob_file = blob_file

    def _fernet(self) -> Fernet:
        if not self.key_file.exists():
            raise FileNotFoundError(f"Master key missing: {self.key_file}. Run `padelbot init-key`.")
        return Fernet(self.key_file.read_bytes())

    def save(self, card: Card) -> None:
        self.blob_file.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = self._fernet().encrypt(json.dumps(asdict(card)).encode())
        self.blob_file.write_bytes(ciphertext)
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
