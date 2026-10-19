from fastapi import APIRouter

from .channels import router as channels_router
from .entries import router as entries_router
from .invocations import router as invocations_router

router = APIRouter()
router.include_router(channels_router)
router.include_router(entries_router)
router.include_router(invocations_router)
