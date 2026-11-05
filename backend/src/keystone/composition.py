"""Explicit code declarations; no discovery, global registration or business table."""

from functools import lru_cache
from importlib import import_module
from pathlib import Path
from types import MappingProxyType

from keystone.core.catalog import Catalog, combine_catalogs, merge_catalogs
from keystone.core.execution.contracts import QueueDefinition
from keystone.core.execution.planning import queue_budgets
from keystone.core.execution.version import artifact_version
from keystone.platform.audit.catalog import CATALOG as AUDIT_CATALOG
from keystone.platform.dictionary.catalog import CATALOG as DICTIONARY_CATALOG
from keystone.platform.files.catalog import CATALOG as FILES_CATALOG
from keystone.platform.flow.catalog import CATALOG as FLOW_CATALOG
from keystone.platform.flow.schemas.packages import build_packages, execution_manifest
from keystone.platform.identity.catalog import CATALOG as IDENTITY_CATALOG
from keystone.platform.model_access.catalog import CATALOG as MODEL_CATALOG
from keystone.worker.catalog import CATALOG as WORKER_CATALOG

PLATFORM_CATALOG = combine_catalogs(
    (
        FLOW_CATALOG,
        IDENTITY_CATALOG,
        AUDIT_CATALOG,
        FILES_CATALOG,
        DICTIONARY_CATALOG,
        MODEL_CATALOG,
        WORKER_CATALOG,
    )
)

# Import each business's lightweight CATALOG here and add (namespace, CATALOG).
BUSINESS_CATALOGS: tuple[tuple[str, Catalog], ...] = ()
FLOW_PACKAGES = ()
FLOW_QUEUES: tuple[QueueDefinition, ...] = ()
# Explicit trusted factories; API never imports business execution modules.
EXECUTION_GUARD = None
WORKFLOW_BINDINGS = ()


def flow_queues(settings):
    definitions = (
        QueueDefinition(
            "keystone-flows", settings.flow_worker_concurrency, settings.flow_global_concurrency
        ),
        *FLOW_QUEUES,
    )
    result = {queue.name: queue for queue in definitions}
    if len(result) != len(definitions):
        raise ValueError("Duplicate Flow queue")
    return result


@lru_cache
def execution_version():
    resources, manifest = execution_manifest(FLOW_PACKAGES)
    return artifact_version(Path(__file__).resolve().parent, resources, manifest=manifest)


def build_flow_definitions(catalog):
    definitions, _ = build_packages(FLOW_PACKAGES, catalog.permissions)
    return definitions


def flow_package_owners():
    return MappingProxyType(
        {
            definition.key: package.key
            for package in FLOW_PACKAGES
            for definition in package.definitions
        }
    )


def register_workflows(runtime=None):
    if WORKFLOW_BINDINGS and runtime is None:
        raise ValueError("Workflow service bindings require the current Runtime")
    for bind in WORKFLOW_BINDINGS:
        bind(runtime)
    for module in sorted({m for package in FLOW_PACKAGES for m in package.execution_modules}):
        _register_module(module)


@lru_cache
def _register_module(module):
    loaded = import_module(module)
    # Explicit factory exports, once per process; imported decorators already run once.
    if hasattr(loaded, "register"):
        loaded.register()


def register_maintenance(runtime, health):
    # Always register guarded entrypoints: persisted ticks may arrive before a
    # disabled plan is paused. No runtime handles or credentials enter inputs.
    from keystone.core.execution import history as history_jobs
    from keystone.platform.files.tasks import workflows as files_jobs
    from keystone.platform.flow.tasks import workflows as flow_jobs
    from keystone.platform.model_access.tasks import workflows as model_jobs

    bindings, installers = [], []
    for module in (files_jobs, flow_jobs, model_jobs):
        tasks, install = module.register(runtime, health)
        bindings.extend(tasks)
        installers.append(install)

    async def prune_history():
        return await runtime.history_retention().execute(apply=True)

    tasks, install = history_jobs.register(
        prune_history, enabled=runtime.settings.history_cleanup_enabled
    )
    bindings.extend(tasks)
    installers.append(install)
    definitions = (
        QueueDefinition(model_jobs.QUEUE, 1, 1),
        QueueDefinition(
            model_jobs.QUERY_QUEUE,
            runtime.settings.model_cost_concurrency,
            runtime.settings.model_cost_concurrency,
        ),
        QueueDefinition(files_jobs.QUEUE, 1, 1),
        QueueDefinition(flow_jobs.QUEUE, 1, 1),
        QueueDefinition(history_jobs.QUEUE, 1, 1),
    )
    queues = queue_budgets(
        bindings,
        definitions,
        (
            files_jobs.CLEANUP_SCHEDULE,
            flow_jobs.RECONCILE_SCHEDULE,
            model_jobs.COST_SCHEDULE,
            history_jobs.HISTORY_SCHEDULE,
        ),
        other_entrypoints=(
            definition.entrypoint for definition in runtime.flow_definitions.values()
        ),
    )
    queues.update(
        {
            name: (queue.worker_concurrency, queue.global_concurrency)
            for name, queue in flow_queues(runtime.settings).items()
        }
    )
    return queues, installers


def build_history_retention(runtime):
    from functools import partial

    from keystone.core.execution.history import HISTORY, HISTORY_SCHEDULE, prune_maintenance
    from keystone.platform.audit.services.retention import prune_audit
    from keystone.platform.files.tasks.definitions import CLEANUP, CLEANUP_SCHEDULE
    from keystone.platform.flow.services.identity import retained_execution_ids, retained_token_ids
    from keystone.platform.flow.tasks.definitions import RECONCILE, RECONCILE_SCHEDULE
    from keystone.platform.identity.services.retention import prune_credentials
    from keystone.platform.model_access.tasks.definitions import (
        COST_SCAN,
        COST_SCHEDULE,
        COST_SCOPE,
    )
    from keystone.use_cases import HistoryRetention

    names = {
        CLEANUP.entrypoint,
        CLEANUP_SCHEDULE.tick_entrypoint,
        RECONCILE.entrypoint,
        RECONCILE_SCHEDULE.tick_entrypoint,
        "keystone.flow.sync_run",
        COST_SCAN.entrypoint,
        COST_SCOPE.entrypoint,
        COST_SCHEDULE.tick_entrypoint,
        HISTORY.entrypoint,
        HISTORY_SCHEDULE.tick_entrypoint,
    }

    async def maintenance(*, apply=False):
        try:
            result = await prune_maintenance(
                runtime.settings,
                names,
                partial(retained_execution_ids, runtime.database),
                apply=apply,
                offset=runtime.history_offset,
            )
        except BaseException:
            runtime.history_offset = 0
            raise
        if apply:
            runtime.history_offset = result["next_offset"]
        return result

    return HistoryRetention(
        partial(prune_credentials, runtime.database, runtime.settings, retained_token_ids),
        partial(prune_audit, runtime.database, runtime.settings.audit_retention_days),
        maintenance,
    )


def build_catalog():
    return merge_catalogs(PLATFORM_CATALOG, BUSINESS_CATALOGS)


def build_model_clients(settings, database):
    from keystone.platform.flow.services.model_references import model_referenced
    from keystone.platform.model_access.adapters.billing.registry import BillingClient
    from keystone.platform.model_access.adapters.channels import CHANNELS
    from keystone.platform.model_access.adapters.clients import ModelClients
    from keystone.platform.model_access.adapters.coordination import QueryCoordination
    from keystone.platform.model_access.adapters.credentials import ChannelCredentials
    from keystone.platform.model_access.adapters.http import HttpClient
    from keystone.platform.model_access.adapters.inference.client import InferenceClient

    http = HttpClient()
    return ModelClients(
        http,
        InferenceClient(http, CHANNELS),
        BillingClient(http, CHANNELS),
        ChannelCredentials(settings.file_coordination_root),
        QueryCoordination(database, settings),
        model_referenced,
    )
