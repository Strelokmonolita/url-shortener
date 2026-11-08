"""Public redirects and asynchronous click recording."""

from fastapi import APIRouter, BackgroundTasks, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import ClickSvc, LinkSvc, get_client_ip
from app.services.click import hash_visitor_ip

router = APIRouter(tags=["redirects"])


@router.get(
    "/{code}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Follow a short link",
    responses={
        302: {"description": "Active link; destination is in the Location header"},
        404: {"description": "Short code does not exist"},
        410: {"description": "Link is disabled or expired"},
    },
)
async def redirect(
    code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    links: LinkSvc,
    clicks: ClickSvc,
) -> RedirectResponse:
    """Send 302 first and then record only successful redirects in a separate session."""
    link = await links.resolve(code)
    # Do not hand a Request, ORM object or raw IP address to the background task.
    background_tasks.add_task(
        clicks.record,
        link_id=link.id,
        visitor_hash=hash_visitor_ip(get_client_ip(request)),
        user_agent=request.headers.get("user-agent"),
        referer=request.headers.get("referer"),
    )
    return RedirectResponse(
        url=link.original_url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
        background=background_tasks,
    )
