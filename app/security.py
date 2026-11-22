"""Small security primitives shared by authentication routes."""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

PASSWORDS = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    return PASSWORDS.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORDS.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _fernet() -> Fernet:
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required before storing tenant secrets.")
    return Fernet(settings.encryption_key.encode("ascii"))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Stored tenant secret cannot be decrypted.") from exc
