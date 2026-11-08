"""Application settings loaded from the environment."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

_DEFAULT_SECRET_KEY = "change-me-in-production"  # noqa: S105 - local development only
_MIN_SIGNING_KEY_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}


class Settings(BaseSettings):
    """Runtime configuration.

    Values are read from environment variables and from a local ``.env`` file.
    Every field can be overridden by the environment, which keeps the container
    image free of any environment-specific defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # Application
    app_name: str = "URL Shortener"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False

    # Server
    host: str = "0.0.0.0"  # noqa: S104 - container binds all interfaces on purpose
    port: int = 8000
    base_url: str = "http://localhost:8000"

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "shortener"
    postgres_password: str = "shortener"  # noqa: S105 - local development default, overridden by .env
    postgres_db: str = "shortener"

    # Security
    secret_key: str = Field(default=_DEFAULT_SECRET_KEY, min_length=8)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_expire_minutes: int = Field(default=15, gt=0)
    refresh_token_expire_days: int = Field(default=30, gt=0)

    # Links
    short_code_length: int = Field(default=7, ge=4, le=16)
    max_custom_alias_length: int = Field(default=32, ge=4, le=64)

    @model_validator(mode="after")
    def validate_deployment_security(self) -> Self:
        """Reject development credentials and debug mode in deployed environments."""
        if self.environment not in {"staging", "production"}:
            return self

        minimum_bytes = _MIN_SIGNING_KEY_BYTES[self.jwt_algorithm]
        if (
            self.secret_key == _DEFAULT_SECRET_KEY
            or len(self.secret_key.encode("utf-8")) < minimum_bytes
        ):
            raise ValueError(
                f"SECRET_KEY must contain at least {minimum_bytes} bytes for "
                f"{self.jwt_algorithm} in staging and production; use a random secret."
            )
        if self.debug:
            raise ValueError("DEBUG must be false in staging and production.")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy connection URL (asyncpg driver)."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Equivalent synchronous SQLAlchemy URL (psycopg2 driver)."""
        url = make_url(self.database_url).set(drivername="postgresql+psycopg2")
        return url.render_as_string(hide_password=False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        """Whether the service runs in a production-grade environment."""
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
