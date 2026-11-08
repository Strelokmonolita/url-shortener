"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import AuthSvc, CurrentUser, UserRepo
from app.schemas.auth import (
    AccessToken,
    RefreshRequest,
    TokenPair,
    UserLogin,
    UserRead,
    UserRegister,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"description": "Email already registered"}},
)
async def register(payload: UserRegister, auth: AuthSvc) -> UserRead:
    """Register a new account and return its public representation."""
    user = await auth.register(email=payload.email, password=payload.password)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Sign in (OAuth2 password flow)",
    responses={401: {"description": "Invalid credentials"}},
)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth: AuthSvc,
) -> TokenPair:
    """Authenticate with form-encoded credentials.

    The ``username`` field carries the email address, which is what the OAuth2
    password flow expects and what the Swagger UI authorisation dialog sends.
    """
    user = await auth.authenticate(email=form.username, password=form.password)
    return auth.issue_tokens(user)


@router.post(
    "/login/json",
    response_model=TokenPair,
    summary="Sign in with a JSON body",
    responses={401: {"description": "Invalid credentials"}},
)
async def login_json(payload: UserLogin, auth: AuthSvc) -> TokenPair:
    """Authenticate with a JSON payload.

    Kept alongside the form endpoint because browser clients and API consumers
    tend to prefer different encodings.
    """
    user = await auth.authenticate(email=payload.email, password=payload.password)
    return auth.issue_tokens(user)


@router.post(
    "/refresh",
    response_model=AccessToken,
    summary="Renew an access token",
    responses={401: {"description": "Refresh token is invalid or expired"}},
)
async def refresh(payload: RefreshRequest, auth: AuthSvc) -> AccessToken:
    """Exchange a refresh token for a fresh access token."""
    _user, access_token = await auth.refresh_access_token(payload.refresh_token)
    return AccessToken(access_token=access_token, expires_in=auth.access_token_lifetime)


@router.get("/me", response_model=UserRead, summary="Current account")
async def read_current_user(user: CurrentUser) -> UserRead:
    """Return the authenticated account."""
    return UserRead.model_validate(user)


@router.get("/exists", summary="Check whether an email is registered")
async def email_registered(email: str, users: UserRepo) -> dict[str, bool]:
    """Report whether an email already has an account.

    Used by the registration form for early feedback.
    """
    return {"exists": await users.email_exists(email)}
