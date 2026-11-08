"""Domain exceptions and their HTTP representation."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for errors the API reports to clients.

    Subclasses carry the status code and a machine-readable code so that the
    service layer never imports FastAPI.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"
    message: str = "Request could not be processed."

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        """Render the error as a JSON-serialisable body."""
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class NotFoundError(AppError):
    """A requested resource does not exist, or is not visible to the caller."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    """The request collides with existing state."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource already exists."


class AuthenticationError(AppError):
    """Credentials are missing, malformed or expired."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Could not validate credentials."


class InvalidURLError(AppError):
    """The submitted URL is not a safe, resolvable target."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "invalid_url"
    message = "The provided URL cannot be shortened."


class GoneError(AppError):
    """The link exists but is no longer available."""

    status_code = status.HTTP_410_GONE
    code = "gone"
    message = "This link is no longer available."


class CodeGenerationError(AppError):
    """A bounded allocation attempt exhausted its generated-code candidates."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "code_generation_failed"
    message = "Could not allocate a unique short code. Please try again."


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """Render an :class:`AppError` as JSON."""
    # Missing codes can be created later; gone links may be re-enabled. Do not
    # let caches retain errors beyond the current request's database state.
    headers = {"Cache-Control": "no-store"}
    if isinstance(exc, AuthenticationError):
        headers.update({"WWW-Authenticate": "Bearer", "Pragma": "no-cache"})
    if isinstance(exc, CodeGenerationError):
        headers["Retry-After"] = "1"
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(),
        headers=headers,
    )


async def validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Report validation locations without echoing credentials or exception objects."""
    fields = [
        {"type": error["type"], "loc": error["loc"], "msg": error["msg"]} for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request payload is invalid.",
                "details": {"fields": fields},
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers that shape every error response."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
