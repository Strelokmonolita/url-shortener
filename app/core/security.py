"""Password hashing and JSON Web Token helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import bcrypt
import jwt

from app.core.config import settings

# bcrypt hashes at most 72 bytes. Passwords are pre-hashed with SHA-256 and the
# digest is encoded as base64 (44 bytes), which keeps every password within the
# limit and removes the truncation edge case entirely.
BCRYPT_ROUNDS: Final = 12


class TokenType(StrEnum):
    """Distinguishes tokens that would otherwise be interchangeable."""

    ACCESS = "access"
    REFRESH = "refresh"


def _prepare(password: str) -> bytes:
    """Reduce a password of any length to 44 bytes bcrypt accepts."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")[:44]


def hash_password(password: str) -> str:
    """Return a salted hash suitable for storage."""
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(_prepare(password), salt).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a password against a stored hash.

    A malformed or truncated stored hash is treated as a failed verification
    rather than an error, so a corrupt row cannot break the login endpoint.
    """
    try:
        result = bcrypt.checkpw(_prepare(plain_password), hashed_password.encode("ascii"))
    except ValueError:
        return False
    return result


def password_needs_rehash(hashed_password: str) -> bool:
    """Report whether a stored hash uses outdated cost parameters."""
    try:
        parts = hashed_password.split("$")
        return int(parts[2]) < BCRYPT_ROUNDS
    except (IndexError, ValueError):
        return True


def _create_token(
    subject: int,
    token_type: TokenType,
    expires_delta: timedelta,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type.value,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(subject: int) -> str:
    """Issue a short-lived access token for ``subject``."""
    return _create_token(
        subject,
        TokenType.ACCESS,
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(subject: int) -> str:
    """Issue a long-lived refresh token for ``subject``."""
    return _create_token(
        subject,
        TokenType.REFRESH,
        timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode ``token`` and assert that it is of ``expected_type``.

    Raises:
        jwt.InvalidTokenError: if the signature, expiry, type or subject are not
            acceptable. Callers translate this into an HTTP error.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "iat", "sub", "type"]},
    )

    if payload.get("type") != expected_type.value:
        msg = f"expected a {expected_type.value} token"
        raise jwt.InvalidTokenError(msg)

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.isdigit():
        msg = "token subject is not a user id"
        raise jwt.InvalidTokenError(msg)

    return payload


def hash_visitor(ip_address: str | None) -> str | None:
    """Derive a stable, non-reversible identifier for a visitor.

    The secret is mixed in so that the digest cannot be reversed with a
    precomputed table covering the IPv4 space.
    """
    if not ip_address:
        return None

    digest = hmac.new(
        settings.secret_key.encode(),
        ip_address.encode(),
        hashlib.sha256,
    )
    return digest.hexdigest()


def generate_short_code(length: int | None = None) -> str:
    """Generate a URL-safe code that avoids visually ambiguous characters."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    size = length or settings.short_code_length
    return "".join(secrets.choice(alphabet) for _ in range(size))
