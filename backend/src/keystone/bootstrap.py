from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response

from keystone import __version__, composition
from keystone.composition import (
    build_catalog,
    build_flow_definitions,
    build_model_clients,
    execution_version,
    flow_package_owners,
)
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.execution.dispatch import TaskClient
from keystone.core.http import install_errors, validate_http_routes
from keystone.core.migrations import expected_heads
from keystone.core.worker_health import worker_status
from keystone.core.worker_identity import WorkerIdentity
from keystone.model_registry import load_models
from keystone.platform.audit.controllers import router as audit_router
from keystone.platform.dictionary.controllers import router as dictionary_router
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferenceService
from keystone.platform.flow.controllers import router as flow_router
from keystone.platform.flow.schemas.definitions import FlowDefinition
from keystone.platform.flow.services.runs import FlowService
from keystone.platform.identity.adapters.security import Passwords
from keystone.platform.identity.controllers import router as identity_router
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.identity.services.login_limits import LoginLimiter
from keystone.platform.model_access.adapters.clients import ModelClients
from keystone.platform.model_access.controllers import router as models_router
from keystone.platform.model_access.services.invocations import InvocationService
from keystone.use_cases import FlowCosts, FlowDiagnostics


@dataclass
class Runtime:
    settings: Settings
    database: Database
    heads: set[str]
    storage: StorageRegistry
    models: ModelClients
    flow_definitions: Mapping[str, FlowDefinition]
    execution_version: Callable[[], str]
    flow_packages: Mapping[str, str] = field(default_factory=dict)
    flow_engine: object | None = None
    worker_identity: WorkerIdentity | None = None
    worker_lease: object | None = None
    execution_guard: Callable | None = None
    history_offset: int = 0
    tasks: TaskClient | None = None

    async def close(self):
        try:
            if self.tasks is not None:
                await self.tasks.close()
        finally:
            try:
                if self.flow_engine is not None:
                    await self.flow_engine.close()
            finally:
                await self.database.close()

    def managed_files(self, identity):
        return FileService(identity, self.storage)

    def model_service(self, identity):
        from keystone.platform.model_access.services.entries import ModelEntryService
        from keystone.platform.model_access.services.runtime import ModelRuntime

        return ModelRuntime(ModelEntryService(identity, self.models))

    def history_retention(self):
        return composition.build_history_retention(self)

    def platform_status(self, identity):
        from functools import partial

        from keystone.platform.files.services.cleanup import backlog as files
        from keystone.platform.flow.services.reconciliation import backlog as flow
        from keystone.platform.model_access.services.costs import backlog as costs
        from keystone.use_cases import PlatformStatus

        return PlatformStatus(
            identity,
            partial(
                worker_status,
                self.database,
                settings=self.settings,
                application_version=self.execution_version(),
            ),
            {
                "files": partial(
                    files,
                    self.database,
                    unreferenced_grace_seconds=self.settings.file_unreferenced_grace_seconds,
                ),
                "flow": partial(flow, self.database),
                "model_costs": partial(costs, self.database),
            },
        )

    def flow_costs(self, identity):
        return FlowCosts(self.flow_service(identity), InvocationService(identity, self.models))

    def flow_diagnostics(self, identity):
        return FlowDiagnostics(
            self.flow_service(identity),
            InvocationService(identity, self.models),
            FileReferenceService(identity),
        )

    def flow_service(self, identity):
        return FlowService(
            identity,
            self.flow_definitions,
            execution_version=self.execution_version,
            package_owners=self.flow_packages,
            queues=composition.flow_queues(self.settings),
        )

    async def readiness(self) -> dict:
        checks = await self.database.readiness(self.heads)
        checks["storage"], sources = await self.storage.readiness()
        return {
            "status": "ready" if all(v == "ok" for v in checks.values()) else "unavailable",
            "checks": checks,
            "storage_sources": sources,
        }


def build_runtime(settings: Settings) -> Runtime:
    if (
        composition.EXECUTION_GUARD is not None and not callable(composition.EXECUTION_GUARD)
    ) or any(not callable(bind) for bind in composition.WORKFLOW_BINDINGS):
        raise ValueError("Business execution hooks must be explicit callables")
    catalog = build_catalog()
    definitions = build_flow_definitions(catalog)
    if {definition.queue for definition in definitions.values()} - composition.flow_queues(
        settings
    ).keys():
        raise ValueError("Flow queue is not configured")
    execution_version()
    load_models()
    heads = expected_heads(settings)
    database = Database(settings, catalog=catalog)
    runtime = Runtime(
        settings,
        database,
        heads,
        StorageRegistry(settings),
        build_model_clients(settings, database),
        definitions,
        execution_version,
        flow_packages=flow_package_owners(),
        execution_guard=composition.EXECUTION_GUARD,
    )
    if settings.database_url:
        from keystone.platform.flow.adapters.dbos import Engine

        runtime.flow_engine = Engine(settings)
        runtime.tasks = TaskClient(settings, application_version=runtime.execution_version)
    return runtime


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        validate_http_routes(app)
        runtime = build_runtime(settings)
        app.state.runtime = runtime
        app.state.passwords = Passwords()
        app.state.login_limiter = LoginLimiter()
        try:
            yield
        finally:
            await runtime.close()

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

    @app.get("/api/v1/worker/status", tags=["health"])
    async def worker_state(request: Request):
        return await app.state.runtime.platform_status(access_context(request)).execute()

    app.include_router(identity_router)
    app.include_router(audit_router)
    app.include_router(dictionary_router)
    app.include_router(models_router)
    app.include_router(flow_router)
    return app
