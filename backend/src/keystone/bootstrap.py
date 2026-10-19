import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Response

from keystone import __version__
from keystone.composition import build_catalog, build_flow_definitions
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.http import install_errors
from keystone.core.migrations import expected_heads
from keystone.model_registry import load_models
from keystone.platform.audit.controllers import router as audit_router
from keystone.platform.dictionary.controllers import router as dictionary_router
from keystone.platform.files.controllers import router as files_router
from keystone.platform.files.services.grants import FileGrantService
from keystone.platform.files.storage import StorageRegistry
from keystone.platform.flow.catalog import Definition
from keystone.platform.flow.controllers import router as flow_router
from keystone.platform.flow.services.runs import FlowService
from keystone.platform.identity.controllers import router as identity_router
from keystone.platform.identity.security import LoginLimiter, Passwords
from keystone.platform.model_access.controllers import router as models_router
from keystone.platform.model_access.gateway import Gateway
from keystone.platform.model_access.services.invocations import InvocationService
from keystone.use_cases import FlowCosts, RoleDeletion


@dataclass
class Runtime:
    settings: Settings
    database: Database
    heads: set[str]
    storage: StorageRegistry
    models: Gateway
    flow_definitions: Mapping[str, Definition]
    flow_engine: object | None = None

    def role_deletion(self, identity):
        return RoleDeletion(identity, FileGrantService(identity.context))

    def flow_costs(self, identity):
        return FlowCosts(
            FlowService(identity, self.flow_definitions), InvocationService(identity, self.models)
        )

    async def readiness(self) -> dict:
        checks = await self.database.readiness(self.heads)
        checks["storage"], sources = await self.storage.readiness()
        return {
            "status": "ready" if all(v == "ok" for v in checks.values()) else "unavailable",
            "checks": checks,
            "storage_sources": sources,
            "flow_enabled": self.settings.flow_enabled,
        }


def build_runtime(settings: Settings) -> Runtime:
    catalog = build_catalog()
    definitions = build_flow_definitions(catalog)
    load_models()
    heads = expected_heads(settings)
    runtime = Runtime(
        settings,
        Database(settings, catalog=catalog),
        heads,
        StorageRegistry(settings),
        Gateway(settings),
        definitions,
    )
    if settings.flow_enabled and settings.database_url:
        from keystone.platform.flow.engine import Engine

        runtime.flow_engine = Engine(settings)
    return runtime


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        runtime = build_runtime(settings)
        app.state.runtime = runtime
        app.state.passwords = Passwords()
        app.state.login_limiter = LoginLimiter()
        app.state.upload_slots = asyncio.Semaphore(4)
        try:
            yield
        finally:
            if runtime.flow_engine:
                await runtime.flow_engine.close()
            await runtime.database.close()

    app = FastAPI(title="Keystone", version=__version__, lifespan=lifespan)
    install_errors(app)

    @app.get("/api/v1/health", tags=["health"])
    async def health(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"status": "ok", "service": "keystone-api", "version": __version__}

    @app.get("/api/v1/ready", tags=["health"])
    async def ready(response: Response):
        result = await app.state.runtime.readiness()
        response.status_code = 200 if result["status"] == "ready" else 503
        response.headers["Cache-Control"] = "no-store"
        return result

    app.include_router(identity_router)
    app.include_router(audit_router)
    app.include_router(files_router)
    app.include_router(dictionary_router)
    app.include_router(models_router)
    app.include_router(flow_router)
    return app
