"""Model tests covering defaults, constraints and cascade behaviour."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Click, Link, User


async def test_user_defaults_are_applied(session: AsyncSession) -> None:
    user = User(email="alice@example.com", hashed_password="hash")
    session.add(user)
    await session.commit()
    await session.refresh(user)

    assert user.id is not None
    assert user.is_active is True
    assert user.created_at is not None


async def test_email_is_unique(session: AsyncSession) -> None:
    session.add(User(email="dup@example.com", hashed_password="hash"))
    await session.commit()

    session.add(User(email="dup@example.com", hashed_password="hash"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_link_code_is_unique(session: AsyncSession) -> None:
    first = User(email="owner@example.com", hashed_password="hash")
    second = User(email="other@example.com", hashed_password="hash")
    session.add_all([first, second])
    await session.commit()

    session.add_all(
        [
            Link(user_id=first.id, code="aB3xY9z", original_url="https://example.com"),
            Link(user_id=second.id, code="aB3xY9z", original_url="https://example.org"),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_click_created_at_has_a_server_default(session: AsyncSession) -> None:
    user = User(email="clicker@example.com", hashed_password="hash")
    session.add(user)
    await session.flush()

    link = Link(user_id=user.id, code="click01", original_url="https://example.com")
    session.add(link)
    await session.flush()

    click = Click(link_id=link.id, user_agent="pytest/1.0", referer="https://ref.example")
    session.add(click)
    await session.commit()
    await session.refresh(click)

    assert click.clicked_at is not None
    assert click.visitor_hash is None


async def test_deleting_a_user_removes_their_links_and_clicks(session: AsyncSession) -> None:
    user = User(email="cascade@example.com", hashed_password="hash")
    session.add(user)
    await session.flush()

    link = Link(user_id=user.id, code="cascade", original_url="https://example.com")
    session.add(link)
    await session.flush()

    session.add(Click(link_id=link.id))
    await session.commit()

    await session.delete(user)
    await session.commit()

    assert (await session.execute(select(Link))).scalars().all() == []
    assert (await session.execute(select(Click))).scalars().all() == []


async def test_relationship_loading_is_explicit(session: AsyncSession) -> None:
    """Lazy loading raises so that missing eager loads surface in tests, not in production."""
    user = User(email="lazy@example.com", hashed_password="hash")
    session.add(user)
    await session.flush()
    session.add(Link(user_id=user.id, code="lazy001", original_url="https://example.com"))
    await session.commit()
    session.expunge_all()

    loaded = (
        await session.execute(select(User).where(User.email == "lazy@example.com"))
    ).scalar_one()

    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = loaded.links
