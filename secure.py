"""Encrypt Telegram sessions at rest (Fernet)."""
import os
from cryptography.fernet import Fernet

_f = Fernet(os.environ["ENCRYPT_KEY"].encode())


def enc(s: str) -> str:
    return _f.encrypt(s.encode()).decode()


def dec(s: str) -> str:
    return _f.decrypt(s.encode()).decode()
