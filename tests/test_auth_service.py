"""Authentication service failure paths and dummy credential verification."""

from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.auth as auth_module
from app.core.exceptions import AuthenticationError, ConflictError
from app.repositories.user import UserRepository
from app.services.auth import AuthService

pytestmark = pytest.mark.usefixtures("fast_password_hashing")


async def test_registration_precheck_avoids_hashing_an_already_registered_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = Mock(spec=UserRepository)
    users.email_exists.return_value = True
    password_hash = Mock()
    monkeypatch.setattr(auth_module, "hash_password", password_hash)

    with pytest.raises(ConflictError):
        await AuthService(cast("UserRepository", users)).register("alice@example.com", "password")

    password_hash.assert_not_called()
    users.create.assert_not_awaited()


async def test_registration_does_not_hide_unrelated_integrity_errors() -> None:
    users = Mock(spec=UserRepository)
    users.email_exists.side_effect = [False, False]
    failure = IntegrityError("INSERT INTO users", None, RuntimeError("unrelated constraint"))
    users.create.side_effect = failure

    with pytest.raises(IntegrityError) as caught:
        await AuthService(cast("UserRepository", users)).register("alice@example.com", "password")

    assert caught.value is failure
    assert users.email_exists.await_count == 2


async def test_unknown_email_still_verifies_a_dummy_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    users = Mock(spec=UserRepository)
    users.get_by_email.return_value = None
    verify = Mock(return_value=False)
    monkeypatch.setattr(auth_module, "verify_password", verify)

    with pytest.raises(AuthenticationError, match="Incorrect email or password"):
        await AuthService(cast("UserRepository", users)).authenticate(
            "unknown@example.com", "wrong"
        )

    verify.assert_called_once_with("wrong", auth_module._DUMMY_HASH)
    users.touch_last_login.assert_not_awaited()
