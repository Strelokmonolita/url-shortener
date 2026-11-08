"""Link creation payloads and public link representations."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
)

from app.core.config import settings
from app.core.links import CODE_PATTERN, MAX_CODE_LENGTH, MAX_URL_LENGTH, as_utc, is_reserved_code


class LinkCreate(BaseModel):
    """An absolute HTTP(S) target with an optional alias and future expiry."""

    model_config = ConfigDict(extra="forbid")

    original_url: HttpUrl = Field(
        ..., max_length=MAX_URL_LENGTH, examples=["https://example.com/articles/first"]
    )
    custom_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CODE_LENGTH,
        pattern=CODE_PATTERN,
        description="Case-sensitive ASCII letters, digits, underscores and hyphens only.",
        examples=["my-first-link"],
    )
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="A future timestamp with a timezone; omit for a non-expiring link.",
    )

    @field_validator("original_url", mode="before")
    @classmethod
    def validate_absolute_url(cls, value: object) -> object:
        """Reject control characters and ambiguous URLs before WHATWG normalization."""
        if not isinstance(value, str):
            return value
        if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value):
            raise ValueError("URL must not contain control characters or backslashes.")
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("URL must be an absolute HTTP(S) URL.")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise ValueError("URL must contain a valid host.") from None
        if not parsed.netloc:
            raise ValueError("URL must contain a valid host.")
        return value

    @field_validator("original_url")
    @classmethod
    def reject_embedded_credentials(cls, value: HttpUrl) -> HttpUrl:
        """Reject credentials and bound the normalized, percent-encoded destination."""
        if value.username is not None or value.password is not None:
            raise ValueError("URL must not contain embedded credentials.")
        if len(str(value)) > MAX_URL_LENGTH:
            raise ValueError(f"Normalized URL must contain at most {MAX_URL_LENGTH} characters.")
        return value

    @field_validator("custom_alias")
    @classmethod
    def validate_custom_alias(cls, value: str | None) -> str | None:
        """Apply the configured length bound and the system-path reservation."""
        if value is None:
            return None
        if is_reserved_code(value):
            raise ValueError("Alias is reserved for a system path.")
        if len(value) > settings.max_custom_alias_length:
            raise ValueError(
                f"Alias must contain at most {settings.max_custom_alias_length} characters."
            )
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        """Accept only future instants and store them in UTC."""
        if value is None:
            return None
        if value <= datetime.now(UTC):
            raise ValueError("Expiry must be in the future.")
        return value.astimezone(UTC)


class LinkRead(BaseModel):
    """A link visible to its authenticated owner."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    code: str
    original_url: HttpUrl = Field(max_length=MAX_URL_LENGTH)
    is_custom_alias: bool
    is_active: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("expires_at", "created_at", "updated_at")
    @classmethod
    def normalise_timestamps(cls, value: datetime | None) -> datetime | None:
        """Expose UTC-aware timestamps on both supported database backends."""
        return as_utc(value) if value is not None else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def short_url(self) -> str:
        """Build a public URL from configuration, never an untrusted Host header."""
        return f"{settings.base_url.rstrip('/')}/{self.code}"
