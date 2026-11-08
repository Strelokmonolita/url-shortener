"""Password format compatibility and strict JWT validation."""

import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import bcrypt
import jwt
import pytest

from app.core import security
from app.core.config import settings
from app.core.security import (
    MAX_TOKEN_LENGTH,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)

pytestmark = pytest.mark.usefixtures("fast_password_hashing")


def _payload(**overrides: Any) -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "sub": "1",
        "type": "access",
        "iat": now - 10,
        "exp": now + 3600,
        "jti": "test-token-id",
        **overrides,
    }


def _sign(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def test_password_hash_uses_the_full_base64_sha256_digest() -> None:
    password = "correct horse battery staple"
    encoded = hash_password(password)
    prepared = base64.b64encode(hashlib.sha256(password.encode()).digest())

    assert encoded.startswith("bcrypt-sha256-v1:$2b$04$")
    assert len(prepared) == 44
    assert bcrypt.checkpw(prepared, encoded.split(":", maxsplit=1)[1].encode())
    assert verify_password(password, encoded)
    assert not verify_password("wrong password", encoded)
    assert not password_needs_rehash(encoded)


def test_hashes_have_independent_salts() -> None:
    assert hash_password("same password") != hash_password("same password")


@pytest.mark.parametrize("password", ["a" * 1024, "пароль🔐" * 30, " a password with spaces "])
def test_long_and_unicode_passwords_round_trip(password: str) -> None:
    encoded = hash_password(password)

    assert verify_password(password, encoded)
    assert not verify_password(password + "!", encoded)


@pytest.mark.parametrize("prefix", ["a" * 72, "🙂" * 24])
def test_password_suffixes_beyond_72_bytes_are_not_truncated(prefix: str) -> None:
    encoded = hash_password(prefix + "first")

    assert verify_password(prefix + "first", encoded)
    assert not verify_password(prefix + "second", encoded)


def test_legacy_password_hashes_remain_verifiable(
    legacy_password_hash: Callable[[str], str],
) -> None:
    encoded = legacy_password_hash("legacy password 🔑")

    assert verify_password("legacy password 🔑", encoded)
    assert not verify_password("another password", encoded)
    assert password_needs_rehash(encoded)


@pytest.mark.parametrize(
    "encoded",
    ["", "not-a-hash", "$2b$12$", "$2b$xx$" + "a" * 53, "bcrypt-sha256-v1:$2b$12$", "🔒"],
)
def test_malformed_hashes_fail_closed(encoded: str) -> None:
    assert not verify_password("password", encoded)
    assert password_needs_rehash(encoded)


def test_invalid_unicode_password_fails_verification_without_crashing() -> None:
    assert not verify_password("bad\ud800password", hash_password("valid password"))


def test_hash_with_lower_cost_needs_rehash(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = hash_password("password")
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 5)

    assert password_needs_rehash(encoded)


def test_hash_with_higher_cost_is_not_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 5)
    encoded = hash_password("password")
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)

    assert not password_needs_rehash(encoded)


def test_older_bcrypt_variant_needs_rehash() -> None:
    encoded = hash_password("password").replace("$2b$", "$2a$")

    assert verify_password("password", encoded)
    assert password_needs_rehash(encoded)


@pytest.mark.parametrize("token_type", list(TokenType))
def test_issued_tokens_have_required_claims_and_configured_lifetime(token_type: TokenType) -> None:
    issue = create_access_token if token_type is TokenType.ACCESS else create_refresh_token
    token = issue(42)
    payload = decode_token(token, expected_type=token_type)
    lifetime = (
        settings.access_token_expire_minutes * 60
        if token_type is TokenType.ACCESS
        else settings.refresh_token_expire_days * 86400
    )

    assert payload["sub"] == "42"
    assert payload["type"] == token_type.value
    assert payload["exp"] - payload["iat"] == lifetime
    assert len(payload["jti"]) == 32
    assert issue(42) != token


@pytest.mark.parametrize("subject", [0, -1, 2_147_483_648, True])
def test_issuance_rejects_invalid_database_user_ids(subject: int) -> None:
    with pytest.raises(ValueError, match="positive database user id"):
        create_access_token(subject)


@pytest.mark.parametrize("token_type", list(TokenType))
def test_access_and_refresh_tokens_are_not_interchangeable(token_type: TokenType) -> None:
    issue = create_refresh_token if token_type is TokenType.ACCESS else create_access_token

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(issue(1), expected_type=token_type)


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "type", "jti"])
def test_decode_requires_every_issued_claim(claim: str) -> None:
    payload = _payload()
    del payload[claim]

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(_sign(payload), expected_type=TokenType.ACCESS)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"sub": "²"}, id="non-decimal-unicode-subject"),
        pytest.param({"sub": "\u0661"}, id="unicode-decimal-subject"),
        pytest.param({"sub": "0"}, id="zero-subject"),
        pytest.param({"sub": "-1"}, id="negative-subject"),
        pytest.param({"sub": "01"}, id="noncanonical-subject"),
        pytest.param({"sub": "2147483648"}, id="out-of-range-subject"),
        pytest.param({"sub": "9" * 100}, id="oversized-subject"),
        pytest.param({"sub": 1}, id="integer-subject"),
        pytest.param({"sub": []}, id="list-subject"),
        pytest.param({"type": "unknown"}, id="unknown-token-type"),
        pytest.param({"type": []}, id="list-token-type"),
        pytest.param({"exp": []}, id="list-expiry"),
        pytest.param({"exp": {}}, id="object-expiry"),
        pytest.param({"exp": "9999999999"}, id="string-expiry"),
        pytest.param({"exp": 9999999999.5}, id="float-expiry"),
        pytest.param({"exp": float("inf")}, id="infinite-expiry"),
        pytest.param({"exp": True}, id="boolean-expiry"),
        pytest.param({"iat": []}, id="list-issued-at"),
        pytest.param({"iat": {}}, id="object-issued-at"),
        pytest.param({"iat": "1"}, id="string-issued-at"),
        pytest.param({"iat": True}, id="boolean-issued-at"),
        pytest.param({"iat": -1}, id="negative-issued-at"),
        pytest.param({"nbf": []}, id="list-not-before"),
        pytest.param({"nbf": {}}, id="object-not-before"),
        pytest.param({"nbf": "1"}, id="string-not-before"),
        pytest.param({"nbf": True}, id="boolean-not-before"),
        pytest.param({"jti": ""}, id="empty-token-id"),
        pytest.param({"jti": " "}, id="blank-token-id"),
        pytest.param({"jti": 1}, id="integer-token-id"),
    ],
)
def test_malformed_claims_raise_jwt_errors_not_conversion_errors(overrides: dict[str, Any]) -> None:
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(_sign(_payload(**overrides)), expected_type=TokenType.ACCESS)


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
def test_decode_checks_expiry_and_future_timestamps(claim: str) -> None:
    now = int(datetime.now(UTC).timestamp())
    value = now - 60 if claim == "exp" else now + 60

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(_sign(_payload(**{claim: value})), expected_type=TokenType.ACCESS)


def test_decode_rejects_wrong_signing_key() -> None:
    token = jwt.encode(
        _payload(), "another-test-signing-secret-" * 3, algorithm=settings.jwt_algorithm
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, expected_type=TokenType.ACCESS)


def test_decode_rejects_disallowed_signing_algorithm() -> None:
    algorithm = "HS384" if settings.jwt_algorithm == "HS256" else "HS256"
    token = jwt.encode(_payload(), settings.secret_key, algorithm=algorithm)

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, expected_type=TokenType.ACCESS)


def test_decode_rejects_unsigned_tokens() -> None:
    token = jwt.encode(_payload(), "", algorithm="none")

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, expected_type=TokenType.ACCESS)


def test_decode_rejects_signature_tampering() -> None:
    header_and_payload, signature = create_access_token(1).rsplit(".", maxsplit=1)
    replacement = "A" if signature[0] != "A" else "B"
    token = f"{header_and_payload}.{replacement}{signature[1:]}"

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, expected_type=TokenType.ACCESS)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "a" * (MAX_TOKEN_LENGTH + 1)])
def test_decode_rejects_malformed_or_oversized_tokens(token: str) -> None:
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, expected_type=TokenType.ACCESS)


def test_decode_accepts_the_largest_database_user_id() -> None:
    payload = decode_token(create_access_token(2_147_483_647), expected_type=TokenType.ACCESS)

    assert payload["sub"] == "2147483647"
