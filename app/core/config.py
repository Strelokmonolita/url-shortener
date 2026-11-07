"""Application settings loaded from the environment."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    secret_key: str = Field(default="change-me-in-production", min_length=8)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_expire_minutes: int = Field(default=15, gt=0)
    refresh_token_expire_days: int = Field(default=30, gt=0)

    # Links
    short_code_length: int = Field(default=7, ge=4, le=16)
    max_custom_alias_length: int = Field(default=32, ge=4, le=64)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy connection URL (asyncpg driver)."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Synchronous connection URL, used by Alembic's offline mode."""
        return self.database_url.replace("+asyncpg", "+psycopg2")

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
