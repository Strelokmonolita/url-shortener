"""Authenticated creation and listing of the current account's links."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import CurrentUser, LinkSvc
from app.schemas.common import Page
from app.schemas.link import LinkCreate, LinkRead


def _disable_caching(response: Response) -> None:
    """Keep owner-specific URLs out of shared HTTP caches."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/links",
    tags=["links"],
    dependencies=[Depends(_disable_caching)],
)


@router.post(
    "",
    response_model=LinkRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a short link",
    responses={
        401: {"description": "A valid access token is required"},
        409: {"description": "Custom alias is already taken"},
        503: {"description": "Could not allocate a unique generated code; retry later"},
    },
)
async def create_link(payload: LinkCreate, user: CurrentUser, links: LinkSvc) -> LinkRead:
    """Create a link owned by the authenticated user, never a client-supplied owner."""
    link = await links.create(user_id=user.id, payload=payload)
    return LinkRead.model_validate(link)


@router.get(
    "",
    response_model=Page[LinkRead],
    summary="List my short links",
    responses={401: {"description": "A valid access token is required"}},
)
async def list_links(
    user: CurrentUser,
    links: LinkSvc,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[LinkRead]:
    """List only this account's links, newest first, including unavailable links."""
    items, total = await links.list_for_user(user.id, limit=limit, offset=offset)
    return Page[LinkRead](
        items=[LinkRead.model_validate(link) for link in items],
        total=total,
        limit=limit,
        offset=offset,
    )
