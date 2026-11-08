"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.security import TokenType, decode_token
from app.db.session import SessionFactory
from app.models import User
from app.repositories.user import UserRepository
from app.services.auth import AuthService

# ``auto_error=False`` lets the dependency raise the API's own error envelope
# instead of FastAPI's default ``{"detail": ...}`` body.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to the current request."""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_user_repository(session: DbSession) -> UserRepository:
    """Build a repository over the request session."""
    return UserRepository(session)


UserRepo = Annotated[UserRepository, Depends(get_user_repository)]


def get_auth_service(users: UserRepo) -> AuthService:
    """Build the authentication service for the current request."""
    return AuthService(users)


AuthSvc = Annotated[AuthService, Depends(get_auth_service)]


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
    """Resolve the visitor's address, honouring a single proxy hop.

    Only the first entry of ``X-Forwarded-For`` is used, and only when the
    application is explicitly deployed behind a proxy; otherwise the socket
    address is authoritative.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.client.host if request.client else None
