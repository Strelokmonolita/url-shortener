"""Authentication API contracts against isolated SQLite and PostgreSQL schemas."""

import json
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.auth as auth_module
from app.api.deps import get_db
from app.core.config import settings
from app.core.security import TokenType, decode_token, hash_password, verify_password
from app.main import create_app
from app.models import User
from app.repositories.user import UserRepository
from app.schemas.auth import PASSWORD_MAX_LENGTH

pytestmark = pytest.mark.usefixtures("fast_password_hashing")
AUTH = "/api/v1/auth"
EMAIL = "alice@example.com"
PASSWORD = "correct horse battery staple 🔐"


@pytest.fixture
def application(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    application = create_app()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_db] = override_db
    return application


@pytest_asyncio.fixture(loop_scope="function")
async def client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        application.router.lifespan_context(application),
        AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client,
    ):
        yield client


@pytest.fixture
async def user(session: AsyncSession) -> User:
    return await UserRepository(session).create(
        email=EMAIL,
        hashed_password=hash_password(PASSWORD),
    )


async def _login(client: AsyncClient, email: str, password: str, encoding: str) -> Response:
    if encoding == "form":
        return await client.post(f"{AUTH}/login", data={"username": email, "password": password})
    return await client.post(f"{AUTH}/login/json", json={"email": email, "password": password})


def _assert_unauthenticated(response: Response) -> None:
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"


async def test_registration_returns_only_public_fields_and_persists_a_hash(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    response = await client.post(
        f"{AUTH}/register", json={"email": "  Alice@EXAMPLE.COM  ", "password": PASSWORD}
    )

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"id", "email", "is_active", "created_at"}
    assert response.json()["email"] == EMAIL
    assert response.json()["is_active"] is True
    assert PASSWORD not in response.text
    stored = (await session.execute(select(User).where(User.email == EMAIL))).scalar_one()
    assert stored.id == response.json()["id"]
    assert stored.last_login_at is None
    assert verify_password(PASSWORD, stored.hashed_password)
    assert stored.hashed_password != PASSWORD


async def test_case_insensitive_duplicate_registration_returns_conflict(
    client: AsyncClient,
    user: User,
) -> None:
    response = await client.post(
        f"{AUTH}/register", json={"email": " ALICE@EXAMPLE.COM ", "password": PASSWORD}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert response.json()["error"]["details"] == {"field": "email"}


async def test_stale_registration_precheck_still_returns_conflict_and_rolls_back(
    client: AsyncClient,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = UserRepository.email_exists
    first_check = True

    async def stale_check(repository: UserRepository, email: str) -> bool:
        nonlocal first_check
        if first_check:
            first_check = False
            return False
        return await original(repository, email)

    monkeypatch.setattr(UserRepository, "email_exists", stale_check)
    response = await client.post(f"{AUTH}/register", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 409
    assert (await client.get(f"{AUTH}/exists", params={"email": EMAIL})).json() == {"exists": True}
    response = await client.post(
        f"{AUTH}/register", json={"email": "other@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": EMAIL, "password": "p@ss7"},
        {"email": EMAIL, "password": "x" * (PASSWORD_MAX_LENGTH + 1)},
        {"email": EMAIL, "password": 123},
        {"email": EMAIL},
        {"password": PASSWORD},
    ],
)
async def test_registration_validation_is_serialisable_and_does_not_echo_input(
    client: AsyncClient,
    payload: dict[str, Any],
) -> None:
    response = await client.post(f"{AUTH}/register", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    fields = response.json()["error"]["details"]["fields"]
    assert fields
    assert all(set(field) == {"type", "loc", "msg"} for field in fields)
    assert PASSWORD not in response.text
    assert "p@ss7" not in response.text


async def test_custom_password_validation_errors_do_not_crash_or_leak_context(
    client: AsyncClient,
) -> None:
    payload = {"email": EMAIL, "password": "bad\ud800password"}
    response = await client.post(
        f"{AUTH}/register",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    field = response.json()["error"]["details"]["fields"][0]
    assert field["loc"] == ["body", "password"]
    assert set(field) == {"type", "loc", "msg"}


@pytest.mark.parametrize("encoding", ["json", "form"])
async def test_login_normalises_email_issues_tokens_and_records_last_login(
    client: AsyncClient,
    session: AsyncSession,
    user: User,
    encoding: str,
) -> None:
    response = await _login(client, " ALICE@EXAMPLE.COM ", PASSWORD, encoding)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    tokens = response.json()
    assert set(tokens) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == settings.access_token_expire_minutes * 60
    for token_type in TokenType:
        assert decode_token(tokens[f"{token_type}_token"], token_type)["sub"] == str(user.id)
    await session.refresh(user)
    assert user.last_login_at is not None

    response = await client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json()["id"] == user.id
    assert set(response.json()) == {"id", "email", "is_active", "created_at"}


@pytest.mark.parametrize("encoding", ["json", "form"])
async def test_both_login_encodings_support_passwords_longer_than_72_bytes(
    client: AsyncClient,
    encoding: str,
) -> None:
    password = "пароль🔐" * 20
    response = await client.post(f"{AUTH}/register", json={"email": EMAIL, "password": password})

    assert response.status_code == 201
    assert (await _login(client, EMAIL, password, encoding)).status_code == 200
    _assert_unauthenticated(await _login(client, EMAIL, password + "!", encoding))


@pytest.mark.parametrize("encoding", ["json", "form"])
@pytest.mark.parametrize("case", ["unknown-email", "wrong-password", "disabled-account"])
async def test_failed_logins_share_one_response_and_do_not_record_a_login(
    client: AsyncClient,
    session: AsyncSession,
    user: User,
    encoding: str,
    case: str,
) -> None:
    email = "unknown@example.com" if case == "unknown-email" else EMAIL
    password = "wrong" if case == "wrong-password" else PASSWORD
    if case == "disabled-account":
        user.is_active = False
        await session.commit()
    response = await _login(client, email, password, encoding)

    _assert_unauthenticated(response)
    assert response.json()["error"]["message"] == "Incorrect email or password."
    await session.refresh(user)
    assert user.last_login_at is None


@pytest.mark.parametrize("encoding", ["json", "form"])
@pytest.mark.parametrize("field", ["email", "password"])
async def test_login_validation_is_consistent_and_uses_the_correct_field_location(
    client: AsyncClient,
    encoding: str,
    field: str,
) -> None:
    email = "not-an-email" if field == "email" else EMAIL
    password = "x" * (PASSWORD_MAX_LENGTH + 1) if field == "password" else PASSWORD
    response = await _login(client, email, password, encoding)

    assert response.status_code == 422
    location = "username" if encoding == "form" and field == "email" else field
    error = response.json()["error"]["details"]["fields"][0]
    assert error["loc"] == ["body", location]
    assert "input" not in error
    assert "ctx" not in error


@pytest.mark.parametrize("correct_password", [False, True])
async def test_legacy_hashes_are_upgraded_only_after_successful_authentication(
    client: AsyncClient,
    session: AsyncSession,
    legacy_password_hash: Callable[[str], str],
    correct_password: bool,
) -> None:
    original_hash = legacy_password_hash(PASSWORD)
    user = await UserRepository(session).create(email=EMAIL, hashed_password=original_hash)
    response = await _login(client, EMAIL, PASSWORD if correct_password else "wrong", "json")
    await session.refresh(user)

    assert response.status_code == (200 if correct_password else 401)
    if correct_password:
        assert user.hashed_password.startswith("bcrypt-sha256-v1:")
        assert verify_password(PASSWORD, user.hashed_password)
        assert user.last_login_at is not None
    else:
        assert user.hashed_password == original_hash
        assert user.last_login_at is None


async def test_bcrypt_work_does_not_run_on_the_request_event_loop(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_thread = threading.get_ident()
    hash_threads: list[int] = []
    verify_threads: list[int] = []

    def track_hash(password: str) -> str:
        hash_threads.append(threading.get_ident())
        return hash_password(password)

    def track_verify(password: str, encoded: str) -> bool:
        verify_threads.append(threading.get_ident())
        return verify_password(password, encoded)

    monkeypatch.setattr(auth_module, "hash_password", track_hash)
    monkeypatch.setattr(auth_module, "verify_password", track_verify)
    assert (
        await client.post(f"{AUTH}/register", json={"email": EMAIL, "password": PASSWORD})
    ).status_code == 201
    assert (await _login(client, EMAIL, PASSWORD, "json")).status_code == 200
    _assert_unauthenticated(await _login(client, "unknown@example.com", PASSWORD, "json"))

    assert hash_threads and verify_threads
    assert all(thread != request_thread for thread in [*hash_threads, *verify_threads])


async def test_refresh_renews_only_access_token_and_is_explicitly_not_rotating(
    client: AsyncClient,
    user: User,
) -> None:
    tokens = (await _login(client, EMAIL, PASSWORD, "json")).json()
    response = await client.post(f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert set(response.json()) == {"access_token", "token_type", "expires_in"}
    access_token = response.json()["access_token"]
    assert access_token != tokens["access_token"]
    assert decode_token(access_token, TokenType.ACCESS)["sub"] == str(user.id)
    repeated = await client.post(f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert repeated.status_code == 200
    assert repeated.json()["access_token"] != access_token


async def test_token_types_cannot_be_substituted_at_http_endpoints(
    client: AsyncClient,
    user: User,
) -> None:
    tokens = (await _login(client, EMAIL, PASSWORD, "json")).json()
    _assert_unauthenticated(
        await client.get(
            f"{AUTH}/me",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )
    )
    _assert_unauthenticated(
        await client.post(f"{AUTH}/refresh", json={"refresh_token": tokens["access_token"]})
    )


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer", "Bearer invalid-token"])
async def test_me_requires_a_valid_bearer_header(
    client: AsyncClient,
    authorization: str | None,
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}

    _assert_unauthenticated(await client.get(f"{AUTH}/me", headers=headers))


@pytest.mark.parametrize("state", ["disabled", "deleted"])
async def test_disabled_and_deleted_accounts_cannot_use_existing_tokens(
    client: AsyncClient,
    session: AsyncSession,
    user: User,
    state: str,
) -> None:
    tokens = (await _login(client, EMAIL, PASSWORD, "json")).json()
    if state == "disabled":
        user.is_active = False
    else:
        await session.delete(user)
    await session.commit()

    _assert_unauthenticated(
        await client.get(
            f"{AUTH}/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    )
    _assert_unauthenticated(
        await client.post(f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]})
    )


@pytest.mark.parametrize("endpoint", ["me", "refresh"])
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"sub": "²"}, id="unicode-subject"),
        pytest.param({"sub": "2147483648"}, id="oversized-user-id"),
        pytest.param({"exp": []}, id="list-expiry"),
        pytest.param({"iat": {}}, id="object-issued-at"),
        pytest.param({"exp": 1}, id="expired-token"),
        pytest.param({"jti": ""}, id="empty-token-id"),
    ],
)
async def test_bad_token_claims_are_http_401_not_500(
    client: AsyncClient,
    user: User,
    endpoint: str,
    overrides: dict[str, Any],
) -> None:
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "sub": str(user.id),
        "type": "access" if endpoint == "me" else "refresh",
        "iat": now - 10,
        "exp": now + 3600,
        "jti": "invalid-http-test-token",
        **overrides,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    response = (
        await client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"})
        if endpoint == "me"
        else await client.post(f"{AUTH}/refresh", json={"refresh_token": token})
    )

    _assert_unauthenticated(response)


async def test_invalid_refresh_string_is_unauthorised(client: AsyncClient) -> None:
    _assert_unauthenticated(
        await client.post(f"{AUTH}/refresh", json={"refresh_token": "not-a-token"})
    )


async def test_exists_preserves_the_public_contract_and_validates_email(
    client: AsyncClient,
    user: User,
) -> None:
    response = await client.get(f"{AUTH}/exists", params={"email": " ALICE@EXAMPLE.COM "})

    assert response.status_code == 200
    assert response.json() == {"exists": True}
    assert response.headers["Cache-Control"] == "no-store"
    assert (await client.get(f"{AUTH}/exists", params={"email": "other@example.com"})).json() == {
        "exists": False
    }
    assert (await client.get(f"{AUTH}/exists", params={"email": "invalid"})).status_code == 422


async def test_openapi_keeps_the_oauth2_password_flow(client: AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()
    flow = document["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"]

    assert flow["tokenUrl"] == f"{AUTH}/login"
    content = document["paths"][f"{AUTH}/login"]["post"]["requestBody"]["content"]
    schema_name = content["application/x-www-form-urlencoded"]["schema"]["$ref"].rsplit("/", 1)[1]
    properties = document["components"]["schemas"][schema_name]["properties"]
    assert {"username", "password"} <= properties.keys()
