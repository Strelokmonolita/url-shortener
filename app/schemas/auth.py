"""Authentication request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# bcrypt hashes at most 72 bytes; rejecting longer input up front gives a clear
# error instead of a surprising truncation.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 72


class UserRegister(BaseModel):
    """Payload accepted by the registration endpoint."""

    email: EmailStr = Field(..., examples=["user@example.com"])
    password: Annotated[
        str,
        Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
    ] = Field(..., examples=["correct horse battery staple"])

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        """Store and compare addresses case-insensitively."""
        return value.strip().lower()


class UserLogin(BaseModel):
    """Payload accepted by the JSON login endpoint."""

    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        """Match the normalisation applied at registration."""
        return value.strip().lower()


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
    token_type: str = "bearer"  # noqa: S105 - OAuth2 field name, not a credential
    expires_in: int = Field(..., description="Access token lifetime in seconds.")


class RefreshRequest(BaseModel):
    """Payload accepted by the token refresh endpoint."""

    refresh_token: str = Field(..., min_length=1)


class AccessToken(BaseModel):
    """Response returned when an access token is renewed."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 field name, not a credential
    expires_in: int
