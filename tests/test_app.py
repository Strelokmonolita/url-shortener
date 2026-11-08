"""Tests for application assembly, liveness and server lifecycle."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from uvicorn.importer import import_from_string

import app.__main__ as entry_point
import app.main as application_module
from app import __version__
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    app_error_handler,
    validation_error_handler,
)
from app.db.session import engine


@pytest.fixture
def application() -> FastAPI:
    return application_module.create_app()


@pytest_asyncio.fixture(loop_scope="function")
async def client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        application.router.lifespan_context(application),
        AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as test_client,
    ):
        yield test_client


def test_asgi_entry_point_is_importable() -> None:
    assert import_from_string("app.main:app") is application_module.app


def test_factory_creates_an_independent_app_with_configured_metadata(application: FastAPI) -> None:
    assert application is not application_module.app
    assert application.title == settings.app_name
    assert application.version == __version__
    assert application.debug == settings.debug


async def test_startup_and_health_do_not_connect_to_the_database(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = Mock(side_effect=AssertionError("Liveness must not connect to the database."))
    monkeypatch.setattr(engine.sync_engine, "connect", connect)

    async with (
        application.router.lifespan_context(application),
        AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client,
    ):
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    connect.assert_not_called()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_documentation_is_available(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 200


async def test_openapi_includes_the_existing_auth_router(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert {
        "/health",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/login/json",
        "/api/v1/auth/refresh",
        "/api/v1/auth/me",
        "/api/v1/auth/exists",
        "/api/v1/links",
        "/{code}",
    } <= paths.keys()


def test_exception_handlers_are_registered(application: FastAPI) -> None:
    assert application.exception_handlers[AppError] is app_error_handler
    assert application.exception_handlers[RequestValidationError] is validation_error_handler


async def test_domain_exception_handler_is_used_for_requests(
    application: FastAPI,
    client: AsyncClient,
) -> None:
    # A single-component route registered after /{code} would be shadowed.
    @application.get("/_tests/conflict")
    async def conflict() -> None:
        raise ConflictError("Test conflict.", field="test")

    response = await client.get("/_tests/conflict")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "conflict",
            "message": "Test conflict.",
            "details": {"field": "test"},
        },
    }


async def test_lifespan_disposes_the_engine_on_shutdown(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispose = AsyncMock()
    monkeypatch.setattr(application_module, "engine", Mock(dispose=dispose))

    async with application_module.lifespan(application):
        dispose.assert_not_awaited()

    dispose.assert_awaited_once_with()


async def test_lifespan_disposes_the_engine_after_an_error(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispose = AsyncMock()
    monkeypatch.setattr(application_module, "engine", Mock(dispose=dispose))

    with pytest.raises(RuntimeError, match="Test failure"):
        async with application_module.lifespan(application):
            raise RuntimeError("Test failure")

    dispose.assert_awaited_once_with()


@pytest.mark.parametrize("debug", [False, True])
def test_local_entry_point_uses_settings(monkeypatch: pytest.MonkeyPatch, debug: bool) -> None:
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    monkeypatch.setattr(settings, "port", 8123)
    monkeypatch.setattr(settings, "debug", debug)

    entry_point.main()

    run.assert_called_once_with(
        "app.main:app",
        host="127.0.0.1",
        port=8123,
        reload=debug,
        log_level="debug" if debug else "info",
        access_log=False,
    )
