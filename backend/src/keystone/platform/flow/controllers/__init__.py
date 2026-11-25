from fastapi import APIRouter

from .model_defaults import router as model_defaults_router
from .runs import router as runs_router

router = APIRouter()
router.include_router(runs_router)
router.include_router(model_defaults_router)
