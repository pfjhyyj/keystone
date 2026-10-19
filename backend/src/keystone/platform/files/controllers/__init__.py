from fastapi import APIRouter

from .files import router as files_router
from .grants import router as grants_router
from .references import router as references_router

router = APIRouter()
router.include_router(files_router)
router.include_router(grants_router)
router.include_router(references_router)
