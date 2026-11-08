"""Link schemas, protected API contracts and collision handling on both databases."""

import asyncio
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import Mock

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.services.link as link_module
from alembic import command
from app.core.config import settings
from app.core.exceptions import ConflictError
from app.core.links import MAX_URL_LENGTH, RESERVED_CODES
from app.core.security import create_refresh_token
from app.models import Link, User
from app.repositories.link import LinkRepository
from app.repositories.user import UserRepository
from app.schemas.link import LinkCreate
from app.services.link import LinkService, generate_code
from tests.database import run_alembic

LINKS = "/api/v1/links"
TARGET = "https://example.com/articles/first?q=hello#section"


@pytest.mark.parametrize(
    "url,normalised",
    [
        ("https://example.com", "https://example.com/"),
        ("  HTTP://EXAMPLE.COM/a b  ", "http://example.com/a%20b"),
        (TARGET, TARGET),
        ("https://пример.рф/path", "https://xn--e1afmkfd.xn--p1ai/path"),
        ("http://localhost:8000/health", "http://localhost:8000/health"),
        ("http://[::1]:8000/path", "http://[::1]:8000/path"),
        ("https://example.com/путь", "https://example.com/%D0%BF%D1%83%D1%82%D1%8C"),
    ],
)
def test_http_urls_are_validated_and_normalised(url: str, normalised: str) -> None:
    payload = LinkCreate(original_url=url)
    assert str(payload.original_url) == normalised
    assert payload.custom_alias is None
    assert payload.expires_at is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "/relative/path",
        "//example.com/path",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hello",
        "http:example.com",
        "https://",
        "https:///example.com",
        "https://[not-ipv6]/",
        "https://example.com:99999/",
        "https://example.com/a\r\nLocation:evil",
        "https://example.com/\x00",
        "https://example.com/\tpath",
        "https://example.com/\x7f",
        "https://example.com\\@evil.example/",
        "https://username:password@example.com/",
        "https://username@example.com/",
        "https://example.com/" + "a" * 2048,
        "https://example.com/" + "💥" * 300,
    ],
)
def test_invalid_or_ambiguous_targets_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        LinkCreate(original_url=url)


def test_url_length_boundary_is_enforced_after_normalisation() -> None:
    prefix = "https://example.com/"
    url = prefix + "a" * (MAX_URL_LENGTH - len(prefix))
    assert str(LinkCreate(original_url=url).original_url) == url
    with pytest.raises(ValidationError):
        LinkCreate(original_url=url + "a")


@pytest.mark.parametrize("alias", [*sorted(RESERVED_CODES), "DOCS", "Health", "API"])
def test_system_aliases_are_reserved_case_insensitively(alias: str) -> None:
    with pytest.raises(ValidationError):
        LinkCreate(original_url=TARGET, custom_alias=alias)


@pytest.mark.parametrize(
    "alias", ["", "has space", " leading", "trailing ", "a/b", "a.b", "a%2fb", "тест", "a\n"]
)
def test_aliases_must_be_single_ascii_path_components(alias: str) -> None:
    with pytest.raises(ValidationError):
        LinkCreate(original_url=TARGET, custom_alias=alias)


def test_alias_length_is_configurable_without_exceeding_the_database_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_custom_alias_length", 4)
    assert LinkCreate(original_url=TARGET, custom_alias="Ab_-").custom_alias == "Ab_-"
    with pytest.raises(ValidationError):
        LinkCreate(original_url=TARGET, custom_alias="abcde")
    monkeypatch.setattr(settings, "max_custom_alias_length", 64)
    assert LinkCreate(original_url=TARGET, custom_alias="x" * 64).custom_alias == "x" * 64
    with pytest.raises(ValidationError):
        LinkCreate(original_url=TARGET, custom_alias="x" * 65)


@pytest.mark.parametrize("length", [4, 7, 16])
def test_generated_codes_use_the_configured_length_and_base62_alphabet(
    monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    monkeypatch.setattr(settings, "short_code_length", length)
    code = generate_code()
    assert len(code) == length
    assert re.fullmatch(r"[A-Za-z0-9]+", code)


@pytest.mark.parametrize(
    "expiry",
    [
        datetime.now(UTC) - timedelta(days=1),
        datetime.now(UTC),
        datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
        "not-a-timestamp",
    ],
)
def test_expiry_requires_a_future_timezone_aware_instant(expiry: Any) -> None:
    with pytest.raises(ValidationError):
        LinkCreate(original_url=TARGET, expires_at=expiry)


def test_future_expiry_is_normalised_to_utc() -> None:
    expiry = datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(days=1)
    payload = LinkCreate(original_url=TARGET, expires_at=expiry)
    assert payload.expires_at == expiry
    assert payload.expires_at is not None
    assert payload.expires_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer invalid-token"])
async def test_link_management_requires_a_valid_access_token(
    api_client: AsyncClient, method: str, authorization: str | None
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await api_client.request(
        method, LINKS, headers=headers, json={"original_url": TARGET} if method == "post" else None
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"


async def test_refresh_tokens_cannot_manage_links(
    api_client: AsyncClient, link_owner: User
) -> None:
    headers = {"Authorization": f"Bearer {create_refresh_token(link_owner.id)}"}
    assert (await api_client.get(LINKS, headers=headers)).status_code == 401
    assert (
        await api_client.post(LINKS, headers=headers, json={"original_url": TARGET})
    ).status_code == 401


async def test_create_generated_link_persists_owner_and_public_fields(
    api_client: AsyncClient,
    session: AsyncSession,
    link_owner: User,
    link_headers: dict[str, str],
) -> None:
    response = await api_client.post(LINKS, headers=link_headers, json={"original_url": TARGET})
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert set(data) == {
        "id",
        "user_id",
        "code",
        "original_url",
        "short_url",
        "is_custom_alias",
        "is_active",
        "expires_at",
        "created_at",
        "updated_at",
    }
    assert data["user_id"] == link_owner.id
    assert data["original_url"] == TARGET
    assert len(data["code"]) == settings.short_code_length
    assert data["short_url"] == f"{settings.base_url.rstrip('/')}/{data['code']}"
    assert data["is_custom_alias"] is False
    assert data["is_active"] is True
    assert data["expires_at"] is None
    for field in ["created_at", "updated_at"]:
        assert datetime.fromisoformat(data[field]).utcoffset() == timedelta(0)
    stored = await session.get(Link, data["id"])
    assert stored is not None
    assert stored.code == data["code"]
    assert stored.user_id == link_owner.id
    assert stored.original_url == TARGET


async def test_custom_alias_expiry_and_configured_base_url_are_returned(
    api_client: AsyncClient,
    link_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "base_url", "https://short.example/prefix/")
    expiry = datetime.now(timezone(timedelta(hours=-3))) + timedelta(days=1)
    response = await api_client.post(
        LINKS,
        headers={**link_headers, "Host": "untrusted.example"},
        json={
            "original_url": "https://example.com",
            "custom_alias": "Launch_2026",
            "expires_at": expiry.isoformat(),
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["code"] == "Launch_2026"
    assert data["is_custom_alias"] is True
    assert data["original_url"] == "https://example.com/"
    assert data["short_url"] == "https://short.example/prefix/Launch_2026"
    assert datetime.fromisoformat(data["expires_at"]) == expiry
    assert datetime.fromisoformat(data["expires_at"]).utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"original_url": "javascript:alert(1)"},
        {"original_url": 123},
        {"original_url": TARGET, "custom_alias": "docs"},
        {"original_url": TARGET, "custom_alias": "has/slash"},
        {"original_url": TARGET, "custom_alias": "x" * 65},
        {"original_url": TARGET, "expires_at": "2000-01-01T00:00:00Z"},
        {"original_url": TARGET, "expires_at": "2999-01-01T00:00:00"},
        {"original_url": TARGET, "user_id": 999},
        {"original_url": TARGET, "is_active": False},
        {"original_url": "https://example.com/" + "💥" * 300},
    ],
)
async def test_invalid_payloads_return_safe_validation_errors(
    api_client: AsyncClient, link_headers: dict[str, str], payload: dict[str, Any]
) -> None:
    response = await api_client.post(LINKS, headers=link_headers, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    fields = response.json()["error"]["details"]["fields"]
    assert all(set(field) == {"type", "loc", "msg"} for field in fields)


@pytest.mark.parametrize("state", ["active", "disabled", "expired"])
async def test_alias_occupancy_is_global_and_unavailable_links_do_not_release_it(
    api_client: AsyncClient,
    session: AsyncSession,
    link_headers: dict[str, str],
    state: str,
) -> None:
    other = await UserRepository(session).create("other-owner@example.com", "unused-hash")
    session.add(
        Link(
            user_id=other.id,
            code="taken",
            original_url=TARGET,
            is_active=state != "disabled",
            expires_at=datetime.now(UTC) - timedelta(days=1) if state == "expired" else None,
        )
    )
    await session.commit()
    response = await api_client.post(
        LINKS, headers=link_headers, json={"original_url": TARGET, "custom_alias": "taken"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"field": "custom_alias"}


@pytest.mark.parametrize("custom", [False, True])
async def test_stale_precheck_rolls_back_and_handles_unique_index_collisions(
    *,
    api_client: AsyncClient,
    session: AsyncSession,
    link_owner: User,
    link_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    custom: bool,
) -> None:
    session.add(Link(user_id=link_owner.id, code="Taken01", original_url=TARGET))
    await session.commit()
    original_precheck = LinkRepository.code_exists
    first_check = True

    async def stale_precheck(repository: LinkRepository, code: str) -> bool:
        nonlocal first_check
        if first_check:
            first_check = False
            return False
        return await original_precheck(repository, code)

    monkeypatch.setattr(LinkRepository, "code_exists", stale_precheck)
    monkeypatch.setattr(link_module, "generate_code", Mock(side_effect=["Taken01", "Fresh01"]))
    payload: dict[str, str] = {"original_url": TARGET}
    if custom:
        payload["custom_alias"] = "Taken01"
    response = await api_client.post(LINKS, headers=link_headers, json=payload)
    assert response.status_code == (409 if custom else 201)
    if not custom:
        assert response.json()["code"] == "Fresh01"
    # A failed insert must not poison subsequent requests or expire an owner ID
    # that a generated-code retry still needs.
    response = await api_client.post(
        LINKS, headers=link_headers, json={"original_url": TARGET, "custom_alias": "after-race"}
    )
    assert response.status_code == 201


async def test_generated_codes_skip_existing_and_reserved_candidates(
    api_client: AsyncClient,
    session: AsyncSession,
    link_owner: User,
    link_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(Link(user_id=link_owner.id, code="Taken01", original_url=TARGET))
    await session.commit()
    generator = Mock(side_effect=["health", "Taken01", "Fresh01"])
    monkeypatch.setattr(link_module, "generate_code", generator)
    response = await api_client.post(LINKS, headers=link_headers, json={"original_url": TARGET})
    assert response.status_code == 201
    assert response.json()["code"] == "Fresh01"
    assert generator.call_count == 3


async def test_generated_code_retries_are_bounded_and_report_a_retryable_error(
    api_client: AsyncClient,
    session: AsyncSession,
    link_owner: User,
    link_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(Link(user_id=link_owner.id, code="Taken01", original_url=TARGET))
    await session.commit()
    generator = Mock(return_value="Taken01")
    monkeypatch.setattr(link_module, "generate_code", generator)
    response = await api_client.post(LINKS, headers=link_headers, json={"original_url": TARGET})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "code_generation_failed"
    assert response.headers["Retry-After"] == "1"
    assert generator.call_count == link_module.MAX_CODE_ATTEMPTS


@pytest.mark.parametrize("custom", [False, True])
async def test_unrelated_integrity_errors_are_not_disguised_as_collisions(custom: bool) -> None:
    repository = Mock(spec=LinkRepository)
    repository.code_exists.side_effect = [False, False]
    failure = IntegrityError("INSERT INTO links", None, RuntimeError("unrelated constraint"))
    repository.create.side_effect = failure
    payload = LinkCreate(original_url=TARGET, custom_alias="chosen" if custom else None)
    with pytest.raises(IntegrityError) as caught:
        await LinkService(cast("LinkRepository", repository)).create(999, payload)
    assert caught.value is failure
    assert repository.create.await_count == 1


async def test_list_is_owner_scoped_paginated_and_stable_when_timestamps_tie(
    api_client: AsyncClient,
    session: AsyncSession,
    link_owner: User,
    link_headers: dict[str, str],
) -> None:
    other = await UserRepository(session).create("other@example.com", "unused-hash")
    timestamp = datetime.now(UTC)
    owned = [
        Link(
            user_id=link_owner.id,
            code=f"own-{index}",
            original_url=TARGET,
            created_at=timestamp,
            is_active=index != 1,
        )
        for index in range(4)
    ]
    session.add_all([*owned, Link(user_id=other.id, code="other", original_url=TARGET)])
    await session.commit()
    expected_ids = sorted((link.id for link in owned), reverse=True)
    response = await api_client.get(LINKS, headers=link_headers, params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["total"] == 4
    assert data["limit"] == 2
    assert data["offset"] == 1
    assert [item["id"] for item in data["items"]] == expected_ids[1:3]
    assert all(item["user_id"] == link_owner.id for item in data["items"])
    assert any(item["is_active"] is False for item in data["items"])
    beyond = await api_client.get(LINKS, headers=link_headers, params={"offset": 99})
    assert beyond.json() == {"items": [], "total": 4, "limit": 20, "offset": 99}


async def test_empty_owner_list_uses_the_shared_page_contract(
    api_client: AsyncClient, link_headers: dict[str, str]
) -> None:
    response = await api_client.get(LINKS, headers=link_headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "bad"}])
async def test_invalid_pagination_is_rejected(
    api_client: AsyncClient, link_headers: dict[str, str], params: dict[str, int | str]
) -> None:
    assert (await api_client.get(LINKS, headers=link_headers, params=params)).status_code == 422


@pytest.mark.postgres
@pytest.mark.parametrize("custom", [False, True])
async def test_real_concurrent_allocations_use_the_unique_index_and_recover(
    postgres_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    async with AsyncSession(postgres_engine, expire_on_commit=False) as setup:
        owner = await UserRepository(setup).create("race-owner@example.com", "unused-hash")
        owner_id = owner.id
    barrier = asyncio.Barrier(2)
    original_precheck = LinkRepository.code_exists

    async def simultaneous_precheck(repository: LinkRepository, code: str) -> bool:
        exists = await original_precheck(repository, code)
        if code == "Race001" and not exists:
            await barrier.wait()
        return exists

    monkeypatch.setattr(LinkRepository, "code_exists", simultaneous_precheck)
    monkeypatch.setattr(
        link_module, "generate_code", Mock(side_effect=["Race001", "Race001", "Next001"])
    )
    payload = LinkCreate(original_url=TARGET, custom_alias="Race001" if custom else None)
    async with (
        AsyncSession(postgres_engine, expire_on_commit=False) as first,
        AsyncSession(postgres_engine, expire_on_commit=False) as second,
    ):
        results = await asyncio.wait_for(
            asyncio.gather(
                LinkService(LinkRepository(first)).create(owner_id, payload),
                LinkService(LinkRepository(second)).create(owner_id, payload),
                return_exceptions=True,
            ),
            timeout=10,
        )
        created = [result for result in results if isinstance(result, Link)]
        assert len(created) == (1 if custom else 2)
        assert sum(isinstance(result, ConflictError) for result in results) == (1 if custom else 0)
        if not custom:
            assert {link.code for link in created} == {"Race001", "Next001"}
        assert first.is_active and second.is_active
        recovery = first if isinstance(results[0], ConflictError) else second
        assert await recovery.scalar(select(func.count(Link.id))) == len(created)
        after = await LinkService(LinkRepository(recovery)).create(
            owner_id, LinkCreate(original_url=TARGET, custom_alias="after-race")
        )
        assert after.code == "after-race"
