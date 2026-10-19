from fastapi import APIRouter

from .dictionaries import router as dictionaries_router

router = APIRouter()
router.include_router(dictionaries_router)
