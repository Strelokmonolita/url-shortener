"""Shared pytest fixtures.

The suite runs against an in-memory SQLite database by default so that a single
``pytest`` invocation needs no external services. Tests marked
``@pytest.mark.postgres`` additionally require an explicit ``TEST_DATABASE_URL``
and are skipped otherwise. PostgreSQL tests use real migrations in randomly
named schemas; they never create/drop the configured database or public tables.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator, Callable

import bcrypt
import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.services.auth as auth_module
from alembic import command
from app.core import security
from app.core.config import settings
from app.db.session import create_engine
from app.models import Base
from tests.database import isolated_postgres_engine, parse_postgres_test_url, run_alembic


def pytest_addoption(parser: pytest.Parser) -> None:
    """Let CI and explicit integration runs reject a missing database URL."""
    parser.addoption(
        "--require-postgres",
        action="store_true",
        help="Fail PostgreSQL tests instead of skipping when TEST_DATABASE_URL is missing.",
    )


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


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ]
)
def database_url(request: pytest.FixtureRequest) -> str | URL:
    """Repeat database-backed contracts on SQLite and migrated PostgreSQL."""
    if request.param == "postgres":
        configured_url: URL = request.getfixturevalue("postgres_url")
        return configured_url
    return "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def engine(database_url: str | URL) -> AsyncIterator[AsyncEngine]:
    """Use ORM-created SQLite or Alembic-migrated, schema-isolated PostgreSQL."""
    if isinstance(database_url, URL):
        async with isolated_postgres_engine(database_url) as test_engine:
            await run_alembic(test_engine, lambda config: command.upgrade(config, "head"))
            yield test_engine
        return

    test_engine = create_engine(database_url)
    try:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield test_engine
    finally:
        # Disposing the final connection destroys this in-memory database.
        await test_engine.dispose()


@pytest.fixture
async def postgres_engine(postgres_url: URL) -> AsyncIterator[AsyncEngine]:
    """Provide an unmigrated schema for migration lifecycle tests."""
    async with isolated_postgres_engine(postgres_url) as test_engine:
        yield test_engine


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
def postgres_url(request: pytest.FixtureRequest) -> URL:
    """Never fall back to application settings or a developer's .env database."""
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        if request.config.getoption("--require-postgres"):
            pytest.fail("TEST_DATABASE_URL is required for PostgreSQL tests.", pytrace=False)
        pytest.skip("TEST_DATABASE_URL is not set")
    try:
        return parse_postgres_test_url(value)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)
