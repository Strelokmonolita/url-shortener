"""Public redirect status codes, background isolation and pseudonymous click events."""

import hashlib
import hmac
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.types import Message, Receive, Scope, Send

import app.services.link as link_module
from alembic import command
from app.api.deps import get_db, get_session_factory
from app.api.redirect import redirect
from app.core.config import settings
from app.core.exceptions import GoneError, NotFoundError
from app.core.links import RESERVED_CODES
from app.main import create_app
from app.models import Click, Link, User
from app.repositories.click import ClickRepository
from app.repositories.link import LinkRepository
from app.repositories.user import UserRepository
from app.services.click import (
    REFERER_MAX_LENGTH,
    USER_AGENT_MAX_LENGTH,
    ClickService,
    hash_visitor_ip,
)
from app.services.link import LinkService
from tests.database import run_alembic

TARGET = "https://example.com/articles/first?q=hello#section"
VISITOR_IP = "203.0.113.7"


async def _clicks(session: AsyncSession) -> list[Click]:
    return list((await session.execute(select(Click).order_by(Click.id))).scalars().all())


async def test_active_link_redirects_publicly_and_records_one_click(
    api_client: AsyncClient, session: AsyncSession, link_headers: dict[str, str]
) -> None:
    created = await api_client.post(
        "/api/v1/links",
        headers=link_headers,
        json={"original_url": TARGET, "custom_alias": "first-link"},
    )
    assert created.status_code == 201
    # No bearer token is sent to the public endpoint.
    response = await api_client.get(
        "/first-link",
        headers={"User-Agent": "test-browser/1.0", "Referer": "https://ref.example/page"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == TARGET
    assert response.headers["Cache-Control"] == "no-store"
    assert response.content == b""
    clicks = await _clicks(session)
    assert len(clicks) == 1
    click = clicks[0]
    assert click.link_id == created.json()["id"]
    assert click.visitor_hash == hash_visitor_ip(VISITOR_IP)
    assert click.visitor_hash is not None
    assert re.fullmatch(r"[0-9a-f]{64}", click.visitor_hash)
    assert VISITOR_IP not in click.visitor_hash
    assert click.user_agent == "test-browser/1.0"
    assert click.referer == "https://ref.example/page"
    assert click.clicked_at is not None


async def test_each_get_records_an_event_and_distinct_addresses_have_distinct_hashes(
    api_client: AsyncClient,
    api_application: FastAPI,
    session: AsyncSession,
    link_owner: User,
) -> None:
    await LinkRepository(session).create(user_id=link_owner.id, code="repeat", original_url=TARGET)
    assert (await api_client.get("/repeat")).status_code == 302
    assert (await api_client.get("/repeat")).status_code == 302
    async with AsyncClient(
        transport=ASGITransport(app=api_application, client=("198.51.100.42", 5000)),
        base_url="http://testserver",
    ) as other_client:
        assert (await other_client.get("/repeat")).status_code == 302
    clicks = await _clicks(session)
    assert len(clicks) == 3
    assert clicks[0].visitor_hash == clicks[1].visitor_hash
    assert clicks[0].visitor_hash != clicks[2].visitor_hash


async def test_missing_code_returns_404_without_a_click(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    response = await api_client.get("/missing-code")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Location" not in response.headers
    assert await _clicks(session) == []


@pytest.mark.parametrize("code", ["DOCS", "API", "HEALTH", "has.dot", "тест", "x" * 65])
async def test_invalid_and_reserved_codes_are_not_public_links(
    api_client: AsyncClient, session: AsyncSession, code: str
) -> None:
    response = await api_client.get(f"/{code}")
    assert response.status_code == 404
    assert "Location" not in response.headers
    assert await _clicks(session) == []


@pytest.mark.parametrize("code", [*sorted(RESERVED_CODES), "a/b", "bad\n", "x" * 65, ""])
async def test_invalid_codes_do_not_even_query_the_link_repository(code: str) -> None:
    repository = Mock(spec=LinkRepository)
    with pytest.raises(NotFoundError):
        await LinkService(cast("LinkRepository", repository)).resolve(code)
    repository.get_by_code.assert_not_awaited()


@pytest.mark.parametrize("state", ["disabled", "expired", "both"])
async def test_unavailable_links_return_410_and_do_not_record_clicks(
    api_client: AsyncClient, session: AsyncSession, link_owner: User, state: str
) -> None:
    session.add(
        Link(
            user_id=link_owner.id,
            code="unavailable",
            original_url=TARGET,
            is_active=state == "expired",
            expires_at=datetime.now(UTC) - timedelta(days=1) if state != "disabled" else None,
        )
    )
    await session.commit()
    response = await api_client.get("/unavailable")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "gone"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Location" not in response.headers
    assert await _clicks(session) == []


async def test_future_expiry_still_redirects(
    api_client: AsyncClient, session: AsyncSession, link_owner: User
) -> None:
    await LinkRepository(session).create(
        user_id=link_owner.id,
        code="future",
        original_url=TARGET,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert (await api_client.get("/future")).status_code == 302
    assert len(await _clicks(session)) == 1


@pytest.mark.parametrize("naive", [False, True])
async def test_expiry_equal_to_now_is_gone_on_both_timestamp_encodings(
    monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    clock = Mock()
    clock.now.return_value = now
    monkeypatch.setattr(link_module, "datetime", clock)
    repository = Mock(spec=LinkRepository)
    repository.get_by_code.return_value = Link(
        id=1,
        user_id=1,
        code="boundary",
        original_url=TARGET,
        is_active=True,
        expires_at=now.replace(tzinfo=None) if naive else now,
    )
    with pytest.raises(GoneError):
        await LinkService(cast("LinkRepository", repository)).resolve("boundary")


async def test_alias_resolution_is_case_sensitive(
    api_client: AsyncClient, session: AsyncSession, link_owner: User
) -> None:
    await LinkRepository(session).create(user_id=link_owner.id, code="AbC_01", original_url=TARGET)
    assert (await api_client.get("/abc_01")).status_code == 404
    assert (await api_client.get("/AbC_01")).status_code == 302
    assert len(await _clicks(session)) == 1


async def test_catch_all_does_not_shadow_docs_health_or_authenticated_api_routes(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = AsyncMock(side_effect=AssertionError("A system route must not resolve a short link."))
    monkeypatch.setattr(LinkRepository, "get_by_code", lookup)
    for path in ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/health"]:
        assert (await api_client.get(path)).status_code == 200
    assert (await api_client.get("/api/v1/links")).status_code == 401
    assert (await api_client.get("/api/v1/auth/me")).status_code == 401
    document = (await api_client.get("/openapi.json")).json()
    assert not document["paths"]["/{code}"]["get"].get("security")
    assert document["paths"]["/api/v1/links"]["get"]["security"]
    assert document["paths"]["/api/v1/links"]["post"]["security"]
    lookup.assert_not_awaited()


async def test_forwarded_headers_cannot_spoof_the_visitor_address(
    api_client: AsyncClient, session: AsyncSession, link_owner: User
) -> None:
    await LinkRepository(session).create(
        user_id=link_owner.id, code="proxy-test", original_url=TARGET
    )
    response = await api_client.get(
        "/proxy-test",
        headers={"X-Forwarded-For": "198.51.100.42, 10.0.0.1", "X-Real-IP": "198.51.100.42"},
    )
    assert response.status_code == 302
    click = (await _clicks(session))[0]
    assert click.visitor_hash == hash_visitor_ip(VISITOR_IP)
    assert click.visitor_hash != hash_visitor_ip("198.51.100.42")


async def test_analytics_headers_are_bounded_and_control_characters_are_removed(
    api_client: AsyncClient, session: AsyncSession, link_owner: User
) -> None:
    await LinkRepository(session).create(
        user_id=link_owner.id, code="metadata", original_url=TARGET
    )
    agent = "browser\x00\t/" + "a" * 600
    referer = "https://ref.example/\x00" + "r" * 3000
    response = await api_client.get("/metadata", headers={"User-Agent": agent, "Referer": referer})
    assert response.status_code == 302
    click = (await _clicks(session))[0]
    assert click.user_agent == ("browser/" + "a" * 600)[:USER_AGENT_MAX_LENGTH]
    assert click.referer == ("https://ref.example/" + "r" * 3000)[:REFERER_MAX_LENGTH]


async def test_missing_client_and_headers_are_stored_as_null_not_fake_visitors(
    api_application: FastAPI, session: AsyncSession, link_owner: User
) -> None:
    await LinkRepository(session).create(user_id=link_owner.id, code="unknown", original_url=TARGET)

    async def without_client(scope: Scope, receive: Receive, send: Send) -> None:
        scope["client"] = None
        await api_application(scope, receive, send)

    async with (
        api_application.router.lifespan_context(api_application),
        AsyncClient(
            transport=ASGITransport(app=without_client), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.get("/unknown", headers={"User-Agent": ""})).status_code == 302
    click = (await _clicks(session))[0]
    assert click.visitor_hash is None
    assert click.user_agent is None
    assert click.referer is None


@pytest.mark.parametrize(
    "failure",
    [
        IntegrityError("private SQL", {"address": VISITOR_IP}, RuntimeError("private details")),
        OperationalError("private SQL", {"address": VISITOR_IP}, RuntimeError("private details")),
        ConnectionRefusedError(f"private details {VISITOR_IP}"),
        RuntimeError(f"private driver error {VISITOR_IP}"),
    ],
)
async def test_analytics_failures_do_not_break_redirects_or_log_request_metadata(
    *,
    api_client: AsyncClient,
    session: AsyncSession,
    link_owner: User,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    link = await LinkRepository(session).create(
        user_id=link_owner.id, code="failure", original_url=TARGET
    )
    monkeypatch.setattr(ClickRepository, "create", AsyncMock(side_effect=failure))
    with caplog.at_level(logging.WARNING, logger="app.services.click"):
        response = await api_client.get(
            "/failure",
            headers={"User-Agent": "private-agent", "Referer": "https://private-ref.example/"},
        )
    assert response.status_code == 302
    assert response.headers["Location"] == TARGET
    assert f"Click recording failed for link {link.id}." in caplog.text
    for private in [VISITOR_IP, "private-agent", "private-ref", "private SQL", "private details"]:
        assert private not in caplog.text
    assert await _clicks(session) == []


async def test_background_session_is_separate_and_request_session_is_closed_first(
    *,
    api_application: FastAPI,
    api_client: AsyncClient,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    link_owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await LinkRepository(session).create(
        user_id=link_owner.id, code="isolated", original_url=TARGET
    )
    request_sessions: list[AsyncSession] = []
    finished_requests: list[AsyncSession] = []
    background_sessions: list[AsyncSession] = []

    async def request_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as request_session:
            request_sessions.append(request_session)
            yield request_session
        finished_requests.append(request_session)

    original_create = ClickRepository.create

    async def track_create(
        repository: ClickRepository,
        *,
        link_id: int,
        visitor_hash: str | None,
        user_agent: str | None,
        referer: str | None,
    ) -> Click:
        assert finished_requests == request_sessions
        assert not request_sessions[0].in_transaction()
        background_sessions.append(repository._session)
        return await original_create(
            repository,
            link_id=link_id,
            visitor_hash=visitor_hash,
            user_agent=user_agent,
            referer=referer,
        )

    api_application.dependency_overrides[get_db] = request_db
    monkeypatch.setattr(ClickRepository, "create", track_create)
    assert (await api_client.get("/isolated")).status_code == 302
    assert len(request_sessions) == len(background_sessions) == 1
    assert request_sessions[0] is not background_sessions[0]
    assert len(await _clicks(session)) == 1


async def test_deletion_between_response_and_background_insert_is_safe(
    api_client: AsyncClient,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    link_owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await LinkRepository(session).create(user_id=link_owner.id, code="deleted", original_url=TARGET)
    original_record = ClickService.record

    async def delete_then_record(
        service: ClickService,
        *,
        link_id: int,
        visitor_hash: str | None,
        user_agent: str | None,
        referer: str | None,
    ) -> None:
        async with session_factory() as deletion_session:
            await deletion_session.execute(delete(Link).where(Link.id == link_id))
            await deletion_session.commit()
        await original_record(
            service,
            link_id=link_id,
            visitor_hash=visitor_hash,
            user_agent=user_agent,
            referer=referer,
        )

    monkeypatch.setattr(ClickService, "record", delete_then_record)
    assert (await api_client.get("/deleted")).status_code == 302
    assert await _clicks(session) == []
    assert (await api_client.get("/deleted")).status_code == 404


async def test_click_repository_rolls_back_a_foreign_key_failure_and_remains_usable(
    session: AsyncSession, link_owner: User
) -> None:
    link = await LinkRepository(session).create(
        user_id=link_owner.id, code="valid", original_url=TARGET
    )
    link_id = link.id
    repository = ClickRepository(session)
    with pytest.raises(IntegrityError):
        await repository.create(link_id=999999, visitor_hash=None, user_agent=None, referer=None)
    click = await repository.create(
        link_id=link_id, visitor_hash=hash_visitor_ip(VISITOR_IP), user_agent="test", referer=None
    )
    assert click.link_id == link_id
    assert click.clicked_at is not None
    assert len(await _clicks(session)) == 1


@pytest.mark.postgres
async def test_redirect_records_a_click_with_a_single_connection_pool(
    postgres_engine: AsyncEngine, postgres_url: URL
) -> None:
    await run_alembic(postgres_engine, lambda config: command.upgrade(config, "head"))
    async with postgres_engine.connect() as connection:
        schema = await connection.scalar(text("SELECT current_schema()"))
        assert isinstance(schema, str)
    limited_engine = create_async_engine(
        postgres_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        factory = async_sessionmaker(limited_engine, expire_on_commit=False, autoflush=False)
        async with factory() as setup:
            owner = await UserRepository(setup).create("pooled@example.com", "unused-hash")
            await LinkRepository(setup).create(user_id=owner.id, code="pooled", original_url=TARGET)
        application = create_app()
        application.dependency_overrides[get_session_factory] = lambda: factory
        async with (
            application.router.lifespan_context(application),
            AsyncClient(
                transport=ASGITransport(app=application), base_url="http://testserver"
            ) as client,
        ):
            assert (await client.get("/pooled")).status_code == 302
        async with factory() as verification:
            assert len(await _clicks(verification)) == 1
    finally:
        await limited_engine.dispose()


def test_ip_hash_is_keyed_domain_separated_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = hmac.new(
        settings.secret_key.encode("utf-8"),
        b"url-shortener:visitor:v1:" + ip_address(VISITOR_IP).packed,
        hashlib.sha256,
    ).hexdigest()
    assert hash_visitor_ip(VISITOR_IP) == expected
    assert expected != hashlib.sha256(VISITOR_IP.encode("ascii")).hexdigest()
    monkeypatch.setattr(settings, "secret_key", "another-test-only-secret-" * 3)
    assert hash_visitor_ip(VISITOR_IP) != expected


@pytest.mark.parametrize(
    "first,second",
    [
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        (VISITOR_IP, f"::ffff:{VISITOR_IP}"),
        ("fe80::1%eth0", "fe80::1%eth1"),
    ],
)
def test_equivalent_ip_encodings_have_the_same_hash(first: str, second: str) -> None:
    assert hash_visitor_ip(first) is not None
    assert hash_visitor_ip(first) == hash_visitor_ip(second)


@pytest.mark.parametrize("value", [None, "", "unknown-client", "999.999.999.999"])
def test_missing_or_invalid_ip_is_not_given_a_synthetic_hash(value: str | None) -> None:
    assert hash_visitor_ip(value) is None


async def test_response_is_sent_before_the_background_task_and_raw_ip_is_not_queued() -> None:
    links = Mock(spec=LinkService)
    links.resolve.return_value = Link(id=1, user_id=1, code="queued", original_url=TARGET)
    clicks = Mock(spec=ClickService)
    messages: list[Message] = []

    async def record(**kwargs: object) -> None:
        assert [message["type"] for message in messages] == [
            "http.response.start",
            "http.response.body",
        ]
        assert "visitor_hash" in kwargs
        assert VISITOR_IP not in kwargs.values()

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    clicks.record.side_effect = record
    scope: Scope = {
        "type": "http",
        "client": (VISITOR_IP, 5000),
        "headers": [],
        "method": "GET",
        "scheme": "http",
        "path": "/queued",
        "query_string": b"",
    }
    background = BackgroundTasks()
    response = await redirect(
        "queued",
        Request(scope),
        background,
        cast("LinkService", links),
        cast("ClickService", clicks),
    )
    assert response.status_code == 302
    assert len(background.tasks) == 1
    assert background.tasks[0].kwargs["visitor_hash"] == hash_visitor_ip(VISITOR_IP)
    assert VISITOR_IP not in background.tasks[0].kwargs.values()
    clicks.record.assert_not_awaited()
    await response(scope, receive, send)
    clicks.record.assert_awaited_once()
