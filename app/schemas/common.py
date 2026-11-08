"""Shared response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Machine-readable error information."""

    code: str
    message: str
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):
    """Envelope used by every failing request."""

    error: ErrorDetail = Field(..., description="What went wrong and why.")


class Page[T](BaseModel):
    """A slice of a larger collection."""

    items: list[T]
    total: int = Field(..., ge=0, description="Total number of matching records.")
    limit: int = Field(..., ge=1, le=100)
    offset: int = Field(..., ge=0)

    @property
    def has_next(self) -> bool:
        """Whether more records follow this page."""
        return self.offset + len(self.items) < self.total


class Message(BaseModel):
    """A plain textual response."""

    detail: str
