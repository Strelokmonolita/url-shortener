"""Database access for short links and owner-scoped collections."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Link


class LinkRepository:
    """Queries over links; the database unique index arbitrates code allocation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, code: str) -> Link | None:
        """Find a public code, including disabled and expired links."""
        statement = select(Link).where(Link.code == code)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def code_exists(self, code: str) -> bool:
        """Check global code occupancy without releasing expired or disabled codes."""
        statement = select(Link.id).where(Link.code == code)
        return (await self._session.execute(statement)).first() is not None

    async def create(
        self,
        *,
        user_id: int,
        code: str,
        original_url: str,
        is_custom_alias: bool = False,
        expires_at: datetime | None = None,
    ) -> Link:
        """Commit a link, rolling back a failed insert before callers retry.

        Raises:
            IntegrityError: if a code is taken concurrently or another constraint
                is violated. Callers must not treat every failure as a collision.
        """
        link = Link(
            user_id=user_id,
            code=code,
            original_url=original_url,
            is_custom_alias=is_custom_alias,
            expires_at=expires_at,
        )
        self._session.add(link)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise
        await self._session.refresh(link)
        return link

    async def list_for_user(self, user_id: int, *, limit: int, offset: int) -> list[Link]:
        """Return only the owner's links, newest first with a stable ID tie-breaker."""
        statement = (
            select(Link)
            .where(Link.user_id == user_id)
            .order_by(Link.created_at.desc(), Link.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        """Count only links owned by the supplied account."""
        statement = select(func.count(Link.id)).where(Link.user_id == user_id)
        return (await self._session.execute(statement)).scalar_one()
