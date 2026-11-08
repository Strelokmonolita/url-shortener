"""Collision-safe short-code allocation and public redirect resolution."""

from __future__ import annotations

import secrets
import string
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.exceptions import CodeGenerationError, ConflictError, GoneError, NotFoundError
from app.core.links import as_utc, is_valid_code
from app.models import Link
from app.repositories.link import LinkRepository
from app.schemas.link import LinkCreate

MAX_CODE_ATTEMPTS = 10
_CODE_ALPHABET = string.ascii_letters + string.digits


def generate_code() -> str:
    """Use cryptographically secure randomness for configurable-length base62 codes."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(settings.short_code_length))


class LinkService:
    """Coordinate link creation, owner lists and redirect availability."""

    def __init__(self, links: LinkRepository) -> None:
        self._links = links

    async def create(self, user_id: int, payload: LinkCreate) -> Link:
        """Allocate a globally unique code without trusting the pre-insert check.

        A custom alias is never silently replaced. Generated codes are retried a
        bounded number of times, including after concurrent unique-index races.
        """
        custom_alias = payload.custom_alias
        attempts = 1 if custom_alias is not None else MAX_CODE_ATTEMPTS
        for _ in range(attempts):
            code = custom_alias if custom_alias is not None else generate_code()
            if not is_valid_code(code):
                continue
            if await self._links.code_exists(code):
                if custom_alias is not None:
                    raise ConflictError("This alias is already taken.", field="custom_alias")
                continue
            try:
                return await self._links.create(
                    user_id=user_id,
                    code=code,
                    original_url=str(payload.original_url),
                    is_custom_alias=custom_alias is not None,
                    expires_at=payload.expires_at,
                )
            except IntegrityError as exc:
                # The repository has rolled back. Recheck occupancy so foreign
                # key violations and other integrity failures are not hidden.
                if not await self._links.code_exists(code):
                    raise
                if custom_alias is not None:
                    raise ConflictError(
                        "This alias is already taken.", field="custom_alias"
                    ) from exc

        raise CodeGenerationError()

    async def list_for_user(
        self, user_id: int, *, limit: int, offset: int
    ) -> tuple[list[Link], int]:
        """Return an owner-scoped page together with its total record count."""
        items = await self._links.list_for_user(user_id, limit=limit, offset=offset)
        total = await self._links.count_for_user(user_id)
        return items, total

    async def resolve(self, code: str) -> Link:
        """Resolve an active public code, distinguishing missing and gone links."""
        if not is_valid_code(code):
            raise NotFoundError("Short link not found.")
        link = await self._links.get_by_code(code)
        if link is None:
            raise NotFoundError("Short link not found.")
        if not link.is_active or (
            link.expires_at is not None and as_utc(link.expires_at) <= datetime.now(UTC)
        ):
            raise GoneError()
        return link
