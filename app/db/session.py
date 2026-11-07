"""Async engine and session factory."""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def create_engine(url: str | None = None, **kwargs: Any) -> AsyncEngine:
    """Build an async engine for ``url`` (defaults to the configured database).

    ``pool_pre_ping`` discards connections dropped by the server between
    requests, which is the usual cause of "connection was closed" errors in
    long-running ASGI workers.
    """
    engine = create_async_engine(
        url or settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        future=True,
        **kwargs,
    )

    if engine.dialect.name == "sqlite":
        _enable_sqlite_foreign_keys(engine)

    return engine


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Turn on foreign key enforcement for SQLite connections.

    SQLite ignores ``ON DELETE CASCADE`` unless the pragma is set per
    connection, which is the default state of the driver. Without this the test
    database would silently behave differently from PostgreSQL.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


engine = create_engine()

# ``expire_on_commit=False`` keeps attributes readable after a commit without
# triggering a lazy refresh, which would fail outside of an awaited context.
SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to the request scope."""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
