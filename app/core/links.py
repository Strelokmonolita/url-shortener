"""Short-code policy and UTC conversion shared by link schemas and services."""

import re
from datetime import UTC, datetime

MAX_CODE_LENGTH = 64
MAX_URL_LENGTH = 2048
CODE_PATTERN = r"^[A-Za-z0-9_-]+$"
RESERVED_CODES = frozenset(
    {
        "admin",
        "api",
        "assets",
        "auth",
        "docs",
        "favicon.ico",
        "health",
        "login",
        "logout",
        "metrics",
        "openapi",
        "openapi.json",
        "ready",
        "redoc",
        "register",
        "robots.txt",
        "static",
    }
)


def is_reserved_code(code: str) -> bool:
    """Reserve system paths irrespective of letter case."""
    return code.casefold() in RESERVED_CODES


def is_valid_code(code: str) -> bool:
    """Accept a single ASCII path component that does not shadow a system route."""
    return (
        1 <= len(code) <= MAX_CODE_LENGTH
        and re.fullmatch(CODE_PATTERN, code) is not None
        and not is_reserved_code(code)
    )


def as_utc(value: datetime) -> datetime:
    """Restore UTC on SQLite timestamps; PostgreSQL already preserves the timezone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
