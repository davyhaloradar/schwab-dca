"""Local Fernet key for encrypting OAuth tokens at rest."""
from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

from app.config import get_settings


def _load_or_create_key() -> bytes:
    settings = get_settings()
    path = Path(settings.token_key_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def get_fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
