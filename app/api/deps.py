"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AuthenticationError
from app.core.security import TokenType, decode_token
from app.db.session import SessionFactory
from app.models import User
from app.repositories.link import LinkRepository
from app.repositories.user import UserRepository
from app.services.auth import AuthService
from app.services.click import ClickService
from app.services.link import LinkService

# ``auto_error=False`` lets the dependency raise the API's own error envelope
# instead of FastAPI's default ``{"detail": ...}`` body.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Share a factory, not a live session, with request and background dependencies."""
    return SessionFactory


DbFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


async def get_db(factory: DbFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session bound to the current request."""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Return the request's connection before sending the response/background tasks.
# Otherwise a full pool can leave every click task waiting on its own request.
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]


def get_user_repository(session: DbSession) -> UserRepository:
    """Build a repository over the request session."""
    return UserRepository(session)


UserRepo = Annotated[UserRepository, Depends(get_user_repository)]


def get_auth_service(users: UserRepo) -> AuthService:
    """Build the authentication service for the current request."""
    return AuthService(users)


AuthSvc = Annotated[AuthService, Depends(get_auth_service)]


def get_link_repository(session: DbSession) -> LinkRepository:
    """Build a link repository over the current request session."""
    return LinkRepository(session)


LinkRepo = Annotated[LinkRepository, Depends(get_link_repository)]


def get_link_service(links: LinkRepo) -> LinkService:
    """Build the link service for the current request."""
    return LinkService(links)


LinkSvc = Annotated[LinkService, Depends(get_link_service)]


def get_click_service(factory: DbFactory) -> ClickService:
    """Give background analytics its own session factory rather than the request session."""
    return ClickService(factory)


ClickSvc = Annotated[ClickService, Depends(get_click_service)]


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    users: UserRepo,
) -> User:
    """Resolve the account identified by the bearer token.

    Raises:
        AuthenticationError: when the header is missing, the token is expired or
            forged, or the account no longer exists or is disabled.
    """
    if not token:
        raise AuthenticationError("Not authenticated.")

    try:
        payload = decode_token(token, expected_type=TokenType.ACCESS)
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Access token is invalid or expired.") from exc

    user = await users.get_by_id(int(payload["sub"]))
    if user is None:
        raise AuthenticationError("Account no longer exists.")
    if not user.is_active:
        raise AuthenticationError("This account is disabled.")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_client_ip(request: Request) -> str | None:
    """Use the ASGI client address, not a user-controlled forwarding header.

    A trusted proxy deployment must configure the ASGI server's proxy middleware
    with an explicit allowlist. That middleware normalizes ``request.client``.
    """
    return request.client.host if request.client else None
