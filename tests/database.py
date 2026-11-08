"""Migration runners and non-destructive PostgreSQL test isolation."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import Connection
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def migration_config(connection: Connection | None = None) -> Config:
    """Locate migrations independently of the caller's working directory."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


async def run_alembic(engine: AsyncEngine, operation: Callable[[Config], None]) -> None:
    """Run a real Alembic command in the engine's transaction and event loop."""
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: operation(migration_config(sync_connection))
        )


def parse_postgres_test_url(value: str) -> URL:
    """Require an explicit asyncpg database URL without exposing credentials."""
    message = "TEST_DATABASE_URL must name a database using postgresql+asyncpg://."
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError(message) from None
    if url.drivername != "postgresql+asyncpg" or not url.database:
        raise ValueError(message)
    return url


@asynccontextmanager
async def isolated_postgres_engine(url: URL) -> AsyncIterator[AsyncEngine]:
    """Create and remove only our own random schema, never a database/public."""
    schema = f"shortener_test_{uuid4().hex}"
    admin_engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))

        try:
            # No public fallback: all unqualified ORM and migration objects
            # belong exclusively to this test. NullPool avoids cross-loop reuse.
            test_engine = create_async_engine(
                url,
                poolclass=NullPool,
                connect_args={"server_settings": {"search_path": schema}},
            )
            try:
                yield test_engine
            finally:
                await test_engine.dispose()
        finally:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
    finally:
        await admin_engine.dispose()
