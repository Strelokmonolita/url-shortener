"""Align last_login_at with the ORM's timezone-aware datetime.

Revision ID: 6c3f9d7a2b10
Revises: dae2356cd32c
Create Date: 2026-11-08

Timezone-naive legacy values are interpreted as UTC. Invalid timestamps abort
the transaction instead of being silently discarded. Downgrades retain each
instant as ISO-formatted UTC text, not necessarily its original spelling.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c3f9d7a2b10"
down_revision: str | None = "dae2356cd32c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert legacy strings without resetting the users table."""
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute("SET LOCAL DateStyle TO 'ISO, YMD'")
    op.alter_column(
        "users",
        "last_login_at",
        existing_type=sa.String(length=40),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="last_login_at::timestamptz",
    )


def downgrade() -> None:
    """Restore the legacy column type while preserving timestamp instants."""
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute("SET LOCAL DateStyle TO 'ISO, YMD'")
    op.alter_column(
        "users",
        "last_login_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.String(length=40),
        existing_nullable=True,
        postgresql_using="last_login_at::varchar(40)",
    )
