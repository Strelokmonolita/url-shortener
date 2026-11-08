"""Click (redirect) event model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPk

if TYPE_CHECKING:
    from app.models.link import Link


class Click(Base):
    """A single redirect served for a link.

    Events are append-only and carry no foreign key to the visitor: the raw IP is
    deliberately not stored, only a keyed hash, which is enough to estimate
    unique visitors without retaining the original address. This is pseudonymous
    data, not a guarantee of anonymity or a count of distinct people.
    """

    __tablename__ = "clicks"
    __table_args__ = (
        # Daily aggregation always filters by link and buckets by time, so this
        # composite index serves both the summary and the time series queries.
        Index("ix_clicks_link_id_clicked_at", "link_id", "clicked_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("links.id", ondelete="CASCADE"),
        nullable=False,
    )
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    referer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Domain-separated HMAC-SHA256 of a canonical IP; 64 hex characters.
    visitor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    link: Mapped[Link] = relationship(back_populates="clicks", lazy="raise")

    def __repr__(self) -> str:
        return f"Click(id={self.id!r}, link_id={self.link_id!r}, clicked_at={self.clicked_at!r})"
