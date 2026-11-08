"""Password hashing and JSON Web Token helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import bcrypt
import jwt

from app.core.config import settings

# bcrypt receives a base64-encoded SHA-256 digest (44 bytes), not the password.
# The prefix distinguishes this format from the previous truncated-hex digest.
BCRYPT_ROUNDS: Final = 12
MAX_TOKEN_LENGTH: Final = 4096
_PASSWORD_HASH_PREFIX: Final = "bcrypt-sha256-v1:"  # noqa: S105 - storage format, not a secret
_BCRYPT_HASH_PATTERN = re.compile(r"\$(2[aby])\$(0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}")
_MAX_USER_ID: Final = 2_147_483_647


class TokenType(StrEnum):
    """Distinguishes tokens that would otherwise be interchangeable."""

    ACCESS = "access"
    REFRESH = "refresh"


def _prepare(password: str) -> bytes:
    """Reduce a password of any length to 44 bytes bcrypt accepts."""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def _prepare_legacy(password: str) -> bytes:
    """Preserve verification of unprefixed hashes written by the original Auth code."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")[:44]


def hash_password(password: str) -> str:
    """Return a salted hash suitable for storage."""
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    encoded = bcrypt.hashpw(_prepare(password), salt).decode("ascii")
    return f"{_PASSWORD_HASH_PREFIX}{encoded}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a password against a stored hash.

    A malformed or truncated stored hash is treated as a failed verification
    rather than an error, so a corrupt row cannot break the login endpoint.
    """
    encoded = hashed_password.removeprefix(_PASSWORD_HASH_PREFIX)
    if _BCRYPT_HASH_PATTERN.fullmatch(encoded) is None:
        return False
    prepare = _prepare if hashed_password.startswith(_PASSWORD_HASH_PREFIX) else _prepare_legacy
    try:
        return bcrypt.checkpw(prepare(plain_password), encoded.encode("ascii"))
    except ValueError:
        return False


def password_needs_rehash(hashed_password: str) -> bool:
    """Report legacy formats, malformed hashes and outdated cost parameters."""
    if not hashed_password.startswith(_PASSWORD_HASH_PREFIX):
        return True
    match = _BCRYPT_HASH_PATTERN.fullmatch(hashed_password.removeprefix(_PASSWORD_HASH_PREFIX))
    return match is None or match[1] != "2b" or int(match[2]) < BCRYPT_ROUNDS


def _create_token(
    subject: int,
    token_type: TokenType,
    expires_delta: timedelta,
) -> str:
    if type(subject) is not int or not 0 < subject <= _MAX_USER_ID:
        raise ValueError("Token subject must be a positive database user id.")
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
    if len(token) > MAX_TOKEN_LENGTH:
        raise jwt.InvalidTokenError("Token is too large.")
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "type", "jti"]},
        )
    except (TypeError, ValueError, OverflowError) as exc:
        # PyJWT converts NumericDate claims with int(); malformed JSON values
        # can otherwise escape as non-JWT exceptions and become HTTP 500s.
        raise jwt.InvalidTokenError("Token claims are malformed.") from exc

    if any(type(payload[claim]) is not int for claim in ("iat", "exp")):
        raise jwt.InvalidTokenError("Token timestamps must be integers.")
    if payload["iat"] < 0 or payload["exp"] <= payload["iat"]:
        raise jwt.InvalidTokenError("Token lifetime is invalid.")
    if "nbf" in payload and type(payload["nbf"]) is not int:
        raise jwt.InvalidTokenError("Token not-before timestamp must be an integer.")
    if not isinstance(payload["jti"], str) or not payload["jti"].strip():
        raise jwt.InvalidTokenError("Token id must be a non-empty string.")

    if payload.get("type") != expected_type.value:
        msg = f"expected a {expected_type.value} token"
        raise jwt.InvalidTokenError(msg)

    subject = payload.get("sub")
    if (
        not isinstance(subject, str)
        or re.fullmatch(r"[1-9][0-9]{0,9}", subject) is None
        or int(subject) > _MAX_USER_ID
    ):
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
