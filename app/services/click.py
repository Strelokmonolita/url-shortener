"""Best-effort background analytics with independent database sessions."""

from __future__ import annotations

import hashlib
import hmac
import logging
from ipaddress import IPv6Address, ip_address

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.repositories.click import ClickRepository

USER_AGENT_MAX_LENGTH = 512
REFERER_MAX_LENGTH = 2048
logger = logging.getLogger(__name__)


def hash_visitor_ip(value: str | None) -> str | None:
    """HMAC a canonical IP with a domain tag; do not retain the address."""
    if value is None:
        return None
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    message = b"url-shortener:visitor:v1:" + address.packed
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _clean_header(value: str | None, maximum: int) -> str | None:
    """Bound analytics input and remove control characters, including PostgreSQL NUL."""
    if value is None:
        return None
    return (
        "".join(char for char in value if ord(char) >= 32 and ord(char) != 127)[:maximum].strip()
        or None
    )


class ClickService:
    """Write events after the response without reusing a request's session."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record(
        self,
        *,
        link_id: int,
        visitor_hash: str | None,
        user_agent: str | None,
        referer: str | None,
    ) -> None:
        """Commit an event in its own session without turning an analytics failure into 500.

        BackgroundTasks is intentionally best-effort, not a durable queue. Log
        only the link ID on failure, not SQL parameters or request metadata.
        """
        try:
            async with self._factory() as session:
                await ClickRepository(session).create(
                    link_id=link_id,
                    visitor_hash=visitor_hash,
                    user_agent=_clean_header(user_agent, USER_AGENT_MAX_LENGTH),
                    referer=_clean_header(referer, REFERER_MAX_LENGTH),
                )
        # This boundary runs after response delivery. Even a driver connection
        # error outside SQLAlchemy's exception hierarchy must not escape it.
        # CancelledError is a BaseException and is deliberately not swallowed.
        except Exception:
            logger.warning("Click recording failed for link %s.", link_id)
