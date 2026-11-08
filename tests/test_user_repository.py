"""User persistence, normalisation and transaction recovery."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user import UserRepository


async def test_user_repository_normalises_writes_and_case_insensitive_queries(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    assert not await users.email_exists("alice@example.com")
    assert await users.get_by_email("alice@example.com") is None
    assert await users.get_by_id(999) is None

    user = await users.create(email="  Alice@EXAMPLE.COM  ", hashed_password="opaque-hash")

    assert user.email == "alice@example.com"
    assert await users.email_exists(" ALICE@EXAMPLE.COM ")
    assert await users.get_by_email(" ALICE@EXAMPLE.COM ") is user
    assert await users.get_by_id(user.id) is user


async def test_failed_duplicate_insert_rolls_back_and_keeps_session_usable(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    await users.create(email="alice@example.com", hashed_password="first-hash")

    with pytest.raises(IntegrityError):
        await users.create(email="ALICE@EXAMPLE.COM", hashed_password="second-hash")

    assert session.is_active
    assert await users.email_exists("alice@example.com")
    other = await users.create(email="other@example.com", hashed_password="other-hash")
    assert other.id is not None


async def test_login_timestamp_and_rehash_are_committed_together(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = UserRepository(session)
    user = await users.create(email="alice@example.com", hashed_password="old-hash")
    commit = AsyncMock(wraps=session.commit)
    monkeypatch.setattr(session, "commit", commit)

    await users.touch_last_login(user, hashed_password="new-hash")

    commit.assert_awaited_once_with()
    await session.refresh(user)
    assert user.last_login_at is not None
    assert user.hashed_password == "new-hash"


async def test_recording_login_without_rehash_keeps_the_password(session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.create(email="alice@example.com", hashed_password="old-hash")

    await users.touch_last_login(user)

    assert user.last_login_at is not None
    assert user.hashed_password == "old-hash"


async def test_password_update_persists_without_recording_login(session: AsyncSession) -> None:
    users = UserRepository(session)
    user = await users.create(email="alice@example.com", hashed_password="old-hash")

    await users.update_password(user, "new-hash")
    await session.refresh(user)

    assert user.hashed_password == "new-hash"
    assert user.last_login_at is None
