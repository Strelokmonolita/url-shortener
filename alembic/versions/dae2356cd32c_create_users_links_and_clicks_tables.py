"""create users, links and clicks tables

Revision ID: dae2356cd32c
Revises:
Create Date: 2026-11-07 22:58:22.523378

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dae2356cd32c"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the users, links and clicks tables."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_login_at", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "links",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("is_custom_alias", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
    )
    op.create_index(op.f("ix_links_code"), "links", ["code"], unique=True)
    op.create_index(op.f("ix_links_user_id"), "links", ["user_id"], unique=False)
    op.create_index("ix_links_user_id_created_at", "links", ["user_id", "created_at"], unique=False)

    op.create_table(
        "clicks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("link_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "clicked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("referer", sa.Text(), nullable=True),
        sa.Column("visitor_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["link_id"],
            ["links.id"],
            name=op.f("fk_clicks_link_id_links"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clicks")),
    )
    op.create_index(
        "ix_clicks_link_id_clicked_at",
        "clicks",
        ["link_id", "clicked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_clicks_visitor_hash"),
        "clicks",
        ["visitor_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the users, links and clicks tables."""
    op.drop_index(op.f("ix_clicks_visitor_hash"), table_name="clicks")
    op.drop_index("ix_clicks_link_id_clicked_at", table_name="clicks")
    op.drop_table("clicks")
    op.drop_index("ix_links_user_id_created_at", table_name="links")
    op.drop_index(op.f("ix_links_user_id"), table_name="links")
    op.drop_index(op.f("ix_links_code"), table_name="links")
    op.drop_table("links")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")

