"""ASGI application and lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.v1.router import router as api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.db.session import engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Release database connections on shutdown without connecting on startup."""
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Build the API without checking or modifying the database schema."""
    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    application.include_router(api_router)

    @application.get("/health", tags=["health"], summary="Application liveness")
    async def health() -> dict[str, str]:
        """Report application liveness, not database or migration readiness."""
        return {"status": "ok"}

    return application


app = create_app()
