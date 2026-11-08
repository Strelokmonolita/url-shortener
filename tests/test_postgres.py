"""Real PostgreSQL migrations, server behaviour and concurrent registration."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alembic import command
from app.core.exceptions import ConflictError
from app.models import User
from app.repositories.user import UserRepository
from app.services.auth import AuthService
from tests.database import isolated_postgres_engine, migration_config, run_alembic

pytestmark = pytest.mark.postgres
INITIAL_REVISION = "dae2356cd32c"


async def _last_login_definition(engine: AsyncEngine) -> tuple[str, int | None, str]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT data_type, character_maximum_length, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'users' AND column_name = 'last_login_at'"
                )
            )
        ).one()
    data_type, maximum_length, nullable = row
    assert isinstance(data_type, str)
    assert maximum_length is None or isinstance(maximum_length, int)
    assert isinstance(nullable, str)
    return data_type, maximum_length, nullable


async def test_fresh_migrations_match_orm_types_defaults_keys_and_indexes(
    postgres_engine: AsyncEngine,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    await run_alembic(postgres_engine, command.check)

    assert await _last_login_definition(postgres_engine) == (
        "timestamp with time zone",
        None,
        "YES",
    )
    expected_head = ScriptDirectory.from_config(migration_config()).get_current_head()
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
        ) == expected_head


async def test_timestamp_upgrade_preserves_legacy_users_links_and_clicks(
    postgres_engine: AsyncEngine,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, INITIAL_REVISION))
    legacy_values: list[tuple[str | None, datetime | None]] = [
        (None, None),
        ("2026-11-07T22:58:22+00:00", datetime(2026, 11, 7, 22, 58, 22, tzinfo=UTC)),
        (
            "2026-11-08T05:40:11.123456+05:30",
            datetime(2026, 11, 8, 0, 10, 11, 123456, tzinfo=UTC),
        ),
        ("2026-11-08T00:40:11-03:30", datetime(2026, 11, 8, 4, 10, 11, tzinfo=UTC)),
        (
            "2026-11-08 10:11:12.345678",
            datetime(2026, 11, 8, 10, 11, 12, 345678, tzinfo=UTC),
        ),
    ]
    user_fields = "SELECT id, email, hashed_password, is_active, created_at FROM users ORDER BY id"
    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (email, hashed_password, is_active, last_login_at) "
                "VALUES (:email, :hashed_password, :is_active, :last_login_at)"
            ),
            [
                {
                    "email": f"legacy-{index}@example.com",
                    "hashed_password": f"legacy-hash-{index}",
                    "is_active": index % 2 == 1,
                    "last_login_at": value,
                }
                for index, (value, _) in enumerate(legacy_values)
            ],
        )
        user_id = await connection.scalar(text("SELECT min(id) FROM users"))
        link_id = await connection.scalar(
            text(
                "INSERT INTO links (user_id, code, original_url) "
                "VALUES (:user_id, 'legacy-link', 'https://example.com/legacy') RETURNING id"
            ),
            {"user_id": user_id},
        )
        await connection.execute(
            text("INSERT INTO clicks (link_id, user_agent) VALUES (:link_id, 'legacy-agent')"),
            {"link_id": link_id},
        )
        original_users = (await connection.execute(text(user_fields))).all()
        original_links = (await connection.execute(text("SELECT * FROM links ORDER BY id"))).all()
        original_clicks = (await connection.execute(text("SELECT * FROM clicks ORDER BY id"))).all()

    # Conversion must not depend on the deployment's session timezone/DateStyle.
    async with postgres_engine.begin() as connection:
        await connection.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
        await connection.execute(text("SET LOCAL DateStyle TO 'SQL, DMY'"))
        await connection.run_sync(
            lambda sync_connection: command.upgrade(migration_config(sync_connection), "head")
        )

    async with postgres_engine.connect() as connection:
        result = await connection.execute(text("SELECT last_login_at FROM users ORDER BY id"))
        timestamps = result.scalars().all()
        assert timestamps == [expected for _, expected in legacy_values]
        assert (await connection.execute(text(user_fields))).all() == original_users
        assert (
            await connection.execute(text("SELECT * FROM links ORDER BY id"))
        ).all() == original_links
        assert (
            await connection.execute(text("SELECT * FROM clicks ORDER BY id"))
        ).all() == original_clicks


async def test_timestamp_downgrade_and_reupgrade_preserve_instants_and_nulls(
    postgres_engine: AsyncEngine,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    timestamp = datetime(2026, 11, 8, 10, 11, 12, 123456, tzinfo=UTC)
    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (email, hashed_password, last_login_at) "
                "VALUES ('roundtrip@example.com', 'unchanged-hash', :timestamp), "
                "('null@example.com', 'null-hash', NULL)"
            ),
            {"timestamp": timestamp},
        )

    async with postgres_engine.begin() as connection:
        await connection.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))
        await connection.execute(text("SET LOCAL DateStyle TO 'SQL, DMY'"))
        await connection.run_sync(
            lambda sync_connection: command.downgrade(
                migration_config(sync_connection), INITIAL_REVISION
            )
        )

    assert await _last_login_definition(postgres_engine) == ("character varying", 40, "YES")
    async with postgres_engine.connect() as connection:
        value = await connection.scalar(
            text("SELECT last_login_at FROM users WHERE email = 'roundtrip@example.com'")
        )
        assert isinstance(value, str)
        assert value.startswith("2026-11-08 ")
        assert len(value) <= 40
        assert datetime.fromisoformat(value) == timestamp
        assert (
            await connection.scalar(
                text("SELECT last_login_at FROM users WHERE email = 'null@example.com'")
            )
            is None
        )

    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    async with postgres_engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT email, hashed_password, last_login_at FROM users ORDER BY id")
            )
        ).all()
        assert rows == [
            ("roundtrip@example.com", "unchanged-hash", timestamp),
            ("null@example.com", "null-hash", None),
        ]


async def test_invalid_legacy_timestamp_aborts_without_changing_data_or_revision(
    postgres_engine: AsyncEngine,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, INITIAL_REVISION))
    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (email, hashed_password, last_login_at) VALUES "
                "('valid@example.com', 'valid-hash', '2026-11-08T10:11:12+00:00'), "
                "('invalid@example.com', 'invalid-hash', 'bad legacy timestamp')"
            )
        )
        original_rows = (await connection.execute(text("SELECT * FROM users ORDER BY id"))).all()

    with pytest.raises(DBAPIError, match="timestamp"):
        await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))

    assert await _last_login_definition(postgres_engine) == ("character varying", 40, "YES")
    async with postgres_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT * FROM users ORDER BY id"))
        ).all() == original_rows
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == INITIAL_REVISION
        )


async def test_full_downgrade_and_reupgrade_work_in_an_empty_isolated_schema(
    postgres_engine: AsyncEngine,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    await run_alembic(postgres_engine, lambda config: command.downgrade(config, "base"))

    async with postgres_engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
        assert tables == ["alembic_version"]
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) is None

    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    await run_alembic(postgres_engine, command.check)


async def test_server_defaults_utc_bigint_ids_and_database_link_cascade(
    postgres_engine: AsyncEngine,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    async with postgres_engine.begin() as connection:
        user_result = await connection.execute(
            text(
                "INSERT INTO users (email, hashed_password) "
                "VALUES ('defaults@example.com', 'opaque-hash') RETURNING *"
            )
        )
        user = user_result.mappings().one()
        await connection.execute(
            text("SELECT setval(pg_get_serial_sequence('links', 'id'), 2147483647)")
        )
        await connection.execute(
            text("SELECT setval(pg_get_serial_sequence('clicks', 'id'), 2147483647)")
        )
        link_result = await connection.execute(
            text(
                "INSERT INTO links (user_id, code, original_url, expires_at) "
                "VALUES (:user_id, 'big-link', 'https://example.com', :expires_at) RETURNING *"
            ),
            {
                "user_id": user["id"],
                "expires_at": datetime.fromisoformat("2026-11-08T17:30:00+05:30"),
            },
        )
        link = link_result.mappings().one()
        click_result = await connection.execute(
            text("INSERT INTO clicks (link_id) VALUES (:link_id) RETURNING *"),
            {"link_id": link["id"]},
        )
        click = click_result.mappings().one()

        assert user["is_active"] is True
        assert user["last_login_at"] is None
        assert link["is_active"] is True
        assert link["is_custom_alias"] is False
        assert link["id"] == 2147483648
        assert click["id"] == 2147483648
        assert click["visitor_hash"] is None
        assert click["user_agent"] is None
        assert click["referer"] is None
        assert link["expires_at"] == datetime(2026, 11, 8, 12, tzinfo=UTC)
        for timestamp in [
            user["created_at"],
            link["created_at"],
            link["updated_at"],
            click["clicked_at"],
        ]:
            assert isinstance(timestamp, datetime)
            assert timestamp.utcoffset() == timedelta(0)

        await connection.execute(text("DELETE FROM links WHERE id = :id"), {"id": link["id"]})
        assert await connection.scalar(text("SELECT count(*) FROM users")) == 1
        assert await connection.scalar(text("SELECT count(*) FROM links")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM clicks")) == 0


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO links (user_id, code, original_url) VALUES (999, 'orphan', 'https://example.com')",
        "INSERT INTO clicks (link_id) VALUES (999)",
    ],
)
async def test_database_foreign_keys_reject_orphans(
    postgres_engine: AsyncEngine,
    statement: str,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))

    with pytest.raises(IntegrityError):
        async with postgres_engine.begin() as connection:
            await connection.execute(text(statement))

    async with postgres_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM links")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM clicks")) == 0


async def test_concurrent_registration_uses_the_unique_index_and_recovers_after_rollback(
    postgres_engine: AsyncEngine,
    fast_password_hashing: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    barrier = asyncio.Barrier(2)
    original_precheck = UserRepository.email_exists

    async def simultaneous_precheck(repository: UserRepository, email: str) -> bool:
        exists = await original_precheck(repository, email)
        if email.strip().lower() == "race@example.com" and not exists:
            await barrier.wait()
        return exists

    monkeypatch.setattr(UserRepository, "email_exists", simultaneous_precheck)
    async with (
        AsyncSession(postgres_engine, expire_on_commit=False) as first,
        AsyncSession(postgres_engine, expire_on_commit=False) as second,
    ):
        results = await asyncio.wait_for(
            asyncio.gather(
                AuthService(UserRepository(first)).register(" RACE@EXAMPLE.COM ", "test-password"),
                AuthService(UserRepository(second)).register("race@example.com", "test-password"),
                return_exceptions=True,
            ),
            timeout=10,
        )

        assert sum(isinstance(result, User) for result in results) == 1
        assert sum(isinstance(result, ConflictError) for result in results) == 1
        assert first.is_active and second.is_active
        failed_session = first if isinstance(results[0], ConflictError) else second
        other = await UserRepository(failed_session).create("other@example.com", "other-hash")
        assert other.email == "other@example.com"
        assert (
            await failed_session.scalar(
                text("SELECT count(*) FROM users WHERE email = 'race@example.com'")
            )
            == 1
        )


async def test_failed_test_schema_cleanup_preserves_a_neighboring_schema(
    postgres_engine: AsyncEngine,
    postgres_url: URL,
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (email, hashed_password) "
                "VALUES ('neighbor@example.com', 'neighbor-hash')"
            )
        )

    schema = ""
    with pytest.raises(RuntimeError, match="test failure"):
        async with isolated_postgres_engine(postgres_url) as temporary_engine:
            async with temporary_engine.connect() as connection:
                schema = await connection.scalar(text("SELECT current_schema()"))
                assert isinstance(schema, str)
                assert schema.startswith("shortener_test_")
                assert await connection.scalar(text("SELECT to_regclass('users')")) is None
                assert await connection.scalar(text("SHOW search_path")) == schema
            await run_alembic(temporary_engine, lambda config: command.upgrade(config, "head"))
            raise RuntimeError("test failure")

    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
                {"schema": schema},
            )
            is False
        )
        assert await connection.scalar(text("SELECT email FROM users")) == "neighbor@example.com"
