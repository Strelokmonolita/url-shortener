"""User account model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.link import Link


class User(Base, TimestampMixin):
    """A registered account.

    Emails are stored lowercase: the registration flow normalises them before
    persisting, so the unique constraint alone is enough to prevent duplicates
    that differ only in case.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    last_login_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    links: Mapped[list[Link]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r})"


# Referenced by the functional index defined in the initial migration, which
# makes case-insensitive lookups use the same index as exact matches.
EMAIL_LOWERCASE_EXPRESSION = func.lower(User.email)
