"""Encrypted on-disk store for OIDC tokens.

Uses Fernet (AES-128-CBC + HMAC) with a master key kept on a separate file.
The key file should be chmod 600 and ideally on a different filesystem path
than the encrypted blob (defense in depth — if one leaks, the other doesn't
unlock anything).
"""
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from cryptography.fernet import Fernet


@dataclass
class TokenBundle:
    access_token: str
    id_token: str
    refresh_token: str | None
    token_type: str
    expires_at: float  # absolute unix timestamp
    scope: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "TokenBundle":
        return cls(**json.loads(raw))


class TokenStore:
    def __init__(self, key_file: Path, blob_file: Path):
        self.key_file = key_file
        self.blob_file = blob_file

    def init_key(self) -> None:
        if self.key_file.exists():
            raise FileExistsError(f"Key file already exists: {self.key_file}")
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self.key_file.write_bytes(key)
        try:
            os.chmod(self.key_file, 0o600)
        except (OSError, NotImplementedError):
            pass  # Windows doesn't fully honor chmod

    def _fernet(self) -> Fernet:
        if not self.key_file.exists():
            raise FileNotFoundError(
                f"Master key not found at {self.key_file}. Run: padelbot init-key"
            )
        return Fernet(self.key_file.read_bytes())

    def save(self, bundle: TokenBundle) -> None:
        self.blob_file.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = self._fernet().encrypt(bundle.to_json().encode())
        self.blob_file.write_bytes(ciphertext)
        try:
            os.chmod(self.blob_file, 0o600)
        except (OSError, NotImplementedError):
            pass

    def load(self) -> TokenBundle | None:
        if not self.blob_file.exists():
            return None
        plaintext = self._fernet().decrypt(self.blob_file.read_bytes())
        return TokenBundle.from_json(plaintext.decode())

    def clear(self) -> None:
        if self.blob_file.exists():
            self.blob_file.unlink()
