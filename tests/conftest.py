"""Shared pytest fixtures.

The suite runs against an in-memory SQLite database by default so that a single
``pytest`` invocation needs no external services. Tests marked
``@pytest.mark.postgres`` additionally require ``TEST_DATABASE_URL`` to point at
a real PostgreSQL instance and are skipped otherwise.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator, Callable, Iterator

import bcrypt
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.services.auth as auth_module
from app.core import security
from app.core.config import settings
from app.db.session import create_engine
from app.models import Base

POSTGRES_TEST_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never sign test tokens with a secret from the developer's .env."""
    monkeypatch.setattr(settings, "secret_key", "test-only-signing-secret-" * 3)


@pytest.fixture
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real bcrypt at a low test-only cost, including dummy verification."""
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)
    monkeypatch.setattr(auth_module, "_DUMMY_HASH", security.hash_password("dummy test password"))


@pytest.fixture
def legacy_password_hash(fast_password_hashing: None) -> Callable[[str], str]:
    """Reproduce the original unprefixed, truncated-hex password format."""

    def make_hash(password: str) -> str:
        prepared = hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")[:44]
        salt = bcrypt.gensalt(rounds=security.BCRYPT_ROUNDS)
        return bcrypt.hashpw(prepared, salt).decode("ascii")

    return make_hash


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run the async test suite on asyncio only."""
    return "asyncio"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Create a throwaway database and drop it when the test finishes."""
    test_engine = create_engine("sqlite+aiosqlite:///:memory:")

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield test_engine

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)

    await test_engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the throwaway database."""
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Session that is rolled back at the end of the test."""
    async with session_factory() as db_session:
        yield db_session
        await db_session.rollback()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Skip the test when no PostgreSQL test database is configured."""
    if not POSTGRES_TEST_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    yield POSTGRES_TEST_URL
