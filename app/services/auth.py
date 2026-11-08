"""Registration, login and token renewal."""

from __future__ import annotations

import jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.models import User
from app.repositories.user import UserRepository
from app.schemas.auth import TokenPair


class AuthService:
    """Coordinates account creation and credential verification."""

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    @property
    def access_token_lifetime(self) -> int:
        """Access token lifetime in seconds, as reported to clients."""
        return settings.access_token_expire_minutes * 60

    async def register(self, email: str, password: str) -> User:
        """Create an account.

        Raises:
            ConflictError: if the email is already taken. The pre-check keeps the
                common case cheap; the unique index remains the source of truth.
        """
        if await self._users.email_exists(email):
            raise ConflictError(
                "An account with this email already exists.",
                field="email",
            )

        return await self._users.create(email=email, hashed_password=hash_password(password))

    async def authenticate(self, email: str, password: str) -> User:
        """Verify credentials and return the matching account.

        Raises:
            AuthenticationError: if the account is unknown, inactive, or the
                password does not match. The same error is used for every case so
                that the endpoint cannot be used to enumerate registered emails.
        """
        user = await self._users.get_by_email(email)
        invalid = AuthenticationError("Incorrect email or password.")

        if user is None:
            # Verify against a dummy hash so that an unknown email costs roughly
            # the same time as a wrong password, which keeps the endpoint from
            # leaking which addresses are registered.
            verify_password(password, _DUMMY_HASH)
            raise invalid

        if not verify_password(password, user.hashed_password):
            raise invalid

        if not user.is_active:
            raise AuthenticationError("This account is disabled.")

        if password_needs_rehash(user.hashed_password):
            await self._users.update_password(user, hash_password(password))

        await self._users.touch_last_login(user)
        return user

    def issue_tokens(self, user: User) -> TokenPair:
        """Mint an access/refresh pair for ``user``."""
        return TokenPair(
            access_token=create_access_token(user.id),
            refresh_token=create_refresh_token(user.id),
            expires_in=self.access_token_lifetime,
        )

    async def refresh_access_token(self, refresh_token: str) -> tuple[User, str]:
        """Exchange a valid refresh token for a new access token.

        Raises:
            AuthenticationError: if the token is expired, forged, of the wrong
                type, or belongs to an account that no longer exists. Token
                rotation is not implemented, so the refresh token stays valid for
                its full lifetime.
        """
        try:
            payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError("Refresh token is invalid or expired.") from exc

        user = await self._users.get_by_id(int(payload["sub"]))
        if user is None or not user.is_active:
            raise AuthenticationError("Refresh token is invalid or expired.")

        return user, create_access_token(user.id)


# A real hash of an arbitrary throwaway string, used only to equalise timing.
_DUMMY_HASH = "$2b$12$4axR47t0hFCr2sWdXaVDuuPFbrlNHeGXn.fLjkIOwtTE6z6fAjVDW"
