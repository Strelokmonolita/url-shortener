"""Version 1 of the public API."""

from fastapi import APIRouter

from app.api.v1 import auth, links

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(links.router)
