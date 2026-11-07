"""Short link model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPk, TimestampMixin, UpdatedAtMixin

if TYPE_CHECKING:
    from app.models.click import Click
    from app.models.user import User


class Link(Base, TimestampMixin, UpdatedAtMixin):
    """A shortened URL owned by a single user.

    ``code`` is the public identifier used in the short URL. It is unique across
    the whole table because redirects are resolved without knowing the owner.
    """

    __tablename__ = "links"
    __table_args__ = (Index("ix_links_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    # True when the user picked the code themselves; used to report back which
    # links can be regenerated with a random code.
    is_custom_alias: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[User] = relationship(back_populates="links", lazy="raise")
    clicks: Mapped[list[Click]] = relationship(
        back_populates="link",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"Link(id={self.id!r}, code={self.code!r}, user_id={self.user_id!r})"
