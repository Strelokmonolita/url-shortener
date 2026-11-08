"""Version 1 of the public API."""

from fastapi import APIRouter

from app.api.v1 import auth

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
