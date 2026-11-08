"""Shared credential validation and public response schemas."""

import pytest
from pydantic import ValidationError

from app.core.security import MAX_TOKEN_LENGTH
from app.schemas.auth import PASSWORD_MAX_LENGTH, RefreshRequest, UserLogin, UserRegister


@pytest.mark.parametrize("schema", [UserLogin, UserRegister])
def test_credentials_normalise_email_without_changing_password(
    schema: type[UserLogin] | type[UserRegister],
) -> None:
    credentials = schema(email="  Alice@EXAMPLE.COM  ", password="  пароль 🔐  ")

    assert credentials.email == "alice@example.com"
    assert credentials.password == "  пароль 🔐  "


@pytest.mark.parametrize("schema", [UserLogin, UserRegister])
def test_credentials_accept_long_unicode_passwords(
    schema: type[UserLogin] | type[UserRegister],
) -> None:
    password = "🔐" * PASSWORD_MAX_LENGTH

    assert schema(email="alice@example.com", password=password).password == password


@pytest.mark.parametrize("schema", [UserLogin, UserRegister])
@pytest.mark.parametrize("password", ["", "x" * (PASSWORD_MAX_LENGTH + 1), "bad\ud800password"])
def test_credentials_reject_empty_oversized_or_invalid_unicode_passwords(
    schema: type[UserLogin] | type[UserRegister],
    password: str,
) -> None:
    with pytest.raises(ValidationError):
        schema(email="alice@example.com", password=password)


def test_registration_requires_eight_characters_but_login_can_check_short_wrong_passwords() -> None:
    with pytest.raises(ValidationError):
        UserRegister(email="alice@example.com", password="short")

    assert UserLogin(email="alice@example.com", password="short").password == "short"


@pytest.mark.parametrize("token", ["", "x" * (MAX_TOKEN_LENGTH + 1)])
def test_refresh_payload_is_bounded(token: str) -> None:
    with pytest.raises(ValidationError):
        RefreshRequest(refresh_token=token)
