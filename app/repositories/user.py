"""Database access for user accounts."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class UserRepository:
    """Queries scoped to the ``users`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: int) -> User | None:
        """Return the account with ``user_id``, if it exists."""
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Return the account with ``email``, matched case-insensitively."""
        statement = select(User).where(func.lower(User.email) == email.strip().lower())
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        """Report whether an account with ``email`` is already registered."""
        statement = select(User.id).where(func.lower(User.email) == email.strip().lower())
        return (await self._session.execute(statement)).first() is not None

    async def create(self, email: str, hashed_password: str) -> User:
        """Insert a new account.

        Raises:
            IntegrityError: if the email was registered concurrently between the
                uniqueness check and this insert.
        """
        user = User(email=email.strip().lower(), hashed_password=hashed_password)
        self._session.add(user)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise
        await self._session.refresh(user)
        return user

    async def touch_last_login(self, user: User) -> None:
        """Record the current moment as the account's last successful login."""
        user.last_login_at = datetime.now(UTC)
        await self._session.commit()

    async def update_password(self, user: User, hashed_password: str) -> None:
        """Replace the stored password hash."""
        user.hashed_password = hashed_password
        await self._session.commit()
