"""Authentication request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import MAX_TOKEN_LENGTH

# Pre-hashing avoids bcrypt's 72-byte limit, including for Unicode passwords.
# Bound both login encodings as well as registration to limit request input.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 1024


class _Credentials(BaseModel):
    """Validation shared by JSON and OAuth2 form credentials."""

    email: EmailStr = Field(..., examples=["user@example.com"])
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        """Store and compare addresses case-insensitively."""
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password_encoding(cls, value: str) -> str:
        """Reject lone Unicode surrogates before password hashing."""
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Password must contain valid Unicode characters.") from exc
        return value


class UserRegister(_Credentials):
    """Payload accepted by the registration endpoint."""

    password: Annotated[
        str,
        Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
    ] = Field(..., examples=["correct horse battery staple"])


class UserLogin(_Credentials):
    """Payload accepted by either login endpoint."""


class UserRead(BaseModel):
    """Public representation of an account."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    """Tokens returned after a successful authentication."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 field, not a credential
    expires_in: int = Field(..., gt=0, description="Access token lifetime in seconds.")


class RefreshRequest(BaseModel):
    """Payload accepted by the token refresh endpoint."""

    refresh_token: str = Field(..., min_length=1, max_length=MAX_TOKEN_LENGTH)


class AccessToken(BaseModel):
    """Response returned when an access token is renewed."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 field, not a credential
    expires_in: int = Field(..., gt=0)
