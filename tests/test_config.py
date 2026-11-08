"""Deployment security settings without reading the developer's .env."""

from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings

Environment = Literal["local", "test", "staging", "production"]
Algorithm = Literal["HS256", "HS384", "HS512"]


@pytest.mark.parametrize("environment", ["local", "test"])
def test_local_and_test_environments_allow_development_settings(environment: Environment) -> None:
    configured = Settings(
        _env_file=None,
        environment=environment,
        secret_key="change-me-in-production",
        debug=True,
    )

    assert configured.environment == environment
    assert configured.debug is True


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("secret", ["change-me-in-production", "x" * 31])
def test_deployed_environments_reject_weak_signing_keys(
    environment: Environment,
    secret: str,
) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY") as caught:
        Settings(
            _env_file=None,
            environment=environment,
            jwt_algorithm="HS256",
            secret_key=secret,
            debug=False,
        )

    assert secret not in str(caught.value)


@pytest.mark.parametrize("algorithm,minimum", [("HS256", 32), ("HS384", 48), ("HS512", 64)])
def test_signing_key_minimum_depends_on_the_algorithm(algorithm: Algorithm, minimum: int) -> None:
    with pytest.raises(ValidationError, match=f"at least {minimum} bytes"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_algorithm=algorithm,
            secret_key="s" * (minimum - 1),
            debug=False,
        )

    configured = Settings(
        _env_file=None,
        environment="production",
        jwt_algorithm=algorithm,
        secret_key="s" * minimum,
        debug=False,
    )
    assert configured.is_production


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_debug_mode_is_rejected_in_deployed_environments(environment: Environment) -> None:
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        Settings(_env_file=None, environment=environment, secret_key="s" * 64, debug=True)


@pytest.mark.parametrize("lifetime", [0, -1])
def test_token_lifetimes_must_be_positive(lifetime: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="test",
            secret_key="test-secret",
            access_token_expire_minutes=lifetime,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="test",
            secret_key="test-secret",
            refresh_token_expire_days=lifetime,
        )


@pytest.mark.parametrize(
    ("username", "password", "database", "host"),
    [
        ("shortener", "shortener", "shortener", "localhost"),
        ("test@user", "test%password/@:# ?", "test database", "localhost"),
        ("test+asyncpg", "literal+asyncpg-password", "db+asyncpg", "localhost"),
        ("пользователь", "пароль🔐", "тест", "localhost"),
        ("shortener", "test-password", "shortener_test", "::1"),
    ],
)
def test_database_urls_preserve_credentials_and_connection_fields(
    username: str,
    password: str,
    database: str,
    host: str,
) -> None:
    configured = Settings(
        _env_file=None,
        environment="test",
        postgres_user=username,
        postgres_password=password,
        postgres_db=database,
        postgres_host=host,
        postgres_port=55432,
    )

    for value, driver in [
        (configured.database_url, "postgresql+asyncpg"),
        (configured.sync_database_url, "postgresql+psycopg2"),
    ]:
        parsed = make_url(value)
        assert parsed.drivername == driver
        assert parsed.username == username
        assert parsed.password == password
        assert parsed.database == database
        assert parsed.host == host
        assert parsed.port == configured.postgres_port
