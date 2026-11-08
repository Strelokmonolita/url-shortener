"""Migration history, offline SQL and PostgreSQL test configuration."""

from io import StringIO
from unittest.mock import MagicMock, Mock

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import Settings, settings
from tests.database import migration_config, parse_postgres_test_url


def test_migration_history_has_one_head_and_retains_the_initial_revision() -> None:
    script = ScriptDirectory.from_config(migration_config())

    assert script.get_bases() == ["dae2356cd32c"]
    assert len(script.get_heads()) == 1
    assert script.get_current_head() != "dae2356cd32c"


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_migrations_work_with_special_passwords_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    password = "offline%password/@:# ?"
    monkeypatch.setattr(settings, "postgres_password", password)
    create_engine = Mock(
        side_effect=AssertionError("Offline migrations must not create an engine.")
    )
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", create_engine)
    output = StringIO()
    config = migration_config()
    config.output_buffer = output

    if direction == "upgrade":
        command.upgrade(config, "head", sql=True)
        expected = "TYPE TIMESTAMP WITH TIME ZONE USING last_login_at::timestamptz"
    else:
        command.downgrade(config, "head:base", sql=True)
        expected = "TYPE VARCHAR(40) USING last_login_at::varchar(40)"

    sql = output.getvalue()
    assert expected in sql
    assert "SET LOCAL TIME ZONE 'UTC'" in sql
    assert "SET LOCAL DateStyle TO 'ISO, YMD'" in sql
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert password not in sql
    create_engine.assert_not_called()


def test_offline_migrations_respect_an_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_default_url(_settings: Settings) -> str:
        raise AssertionError("An explicit migration URL must not consult application settings.")

    monkeypatch.setattr(Settings, "database_url", property(reject_default_url))
    config = migration_config()
    config.set_main_option("sqlalchemy.url", "postgresql://offline")
    output = StringIO()
    config.output_buffer = output

    command.upgrade(config, "head", sql=True)

    assert "CREATE TABLE users" in output.getvalue()
    assert "USING last_login_at::timestamptz" in output.getvalue()


@pytest.mark.parametrize("explicit_url", [False, True])
def test_online_migrations_resolve_the_url_and_dispose_the_engine_after_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
    explicit_url: bool,
) -> None:
    monkeypatch.setattr(settings, "postgres_password", "online%password/@:# ?")
    config = migration_config()
    expected_url = settings.database_url
    if explicit_url:
        expected_url = "postgresql+asyncpg://offline/shortener_test"
        config.set_main_option("sqlalchemy.url", expected_url)

    owned_engine = MagicMock(spec=AsyncEngine)
    owned_engine.connect.return_value.__aenter__.side_effect = RuntimeError(
        "test connection failure"
    )
    factory = Mock(return_value=owned_engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", factory)

    with pytest.raises(RuntimeError, match="test connection failure"):
        command.upgrade(config, "head")

    factory.assert_called_once_with(expected_url, poolclass=NullPool)
    owned_engine.dispose.assert_awaited_once_with()


@pytest.mark.parametrize(
    "value",
    [
        "not a URL",
        "sqlite+aiosqlite:///:memory:",
        "postgresql://user:private-password@localhost/shortener_test",
        "postgresql+psycopg2://user:private-password@localhost/shortener_test",
        "postgresql+asyncpg://user:private-password@localhost",
        "postgresql+asyncpg://user:private-password@localhost:invalid/shortener_test",
    ],
)
def test_postgres_test_urls_reject_invalid_or_implicit_configuration(value: str) -> None:
    with pytest.raises(ValueError, match="TEST_DATABASE_URL") as caught:
        parse_postgres_test_url(value)

    assert "private-password" not in str(caught.value)


def test_postgres_test_url_preserves_credentials_but_hides_them_in_repr() -> None:
    url = parse_postgres_test_url(
        "postgresql+asyncpg://user:private%40password@localhost:5433/shortener_test"
    )

    assert url.drivername == "postgresql+asyncpg"
    assert url.database == "shortener_test"
    assert url.password == "private@password"
    assert "private" not in repr(url)
