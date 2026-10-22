"""Explicit code declarations; no discovery, global registration or business table."""

from functools import lru_cache
from pathlib import Path

from keystone.core.catalog import Catalog, merge_catalogs
from keystone.core.execution.contracts import QueueDefinition
from keystone.core.execution.planning import queue_budgets
from keystone.core.execution.version import artifact_version
from keystone.platform.audit.services.writer import ACTIONS
from keystone.platform.flow.catalog import build_definitions
from keystone.platform.identity.policy import PERMISSIONS

PLATFORM_CATALOG = Catalog(PERMISSIONS, ACTIONS)

# Import each business's lightweight CATALOG here and add (namespace, CATALOG).
BUSINESS_CATALOGS: tuple[tuple[str, Catalog], ...] = ()
FLOW_DEFINITIONS = ()
# Package import name -> required resource paths. API and Worker hash the same files.
EXECUTION_PACKAGES: dict[str, tuple[str, ...]] = {}


@lru_cache
def execution_version():
    return artifact_version(Path(__file__).resolve().parent, EXECUTION_PACKAGES)


def build_flow_definitions(catalog):
    return build_definitions(FLOW_DEFINITIONS, catalog.permissions)


def register_workflows():
    # Application composition adds Worker-only imports here.
    pass


def register_maintenance(runtime, health):
    # Always register guarded entrypoints: persisted ticks may arrive before a
    # disabled plan is paused. No runtime handles or credentials enter inputs.
    from keystone.platform.files.tasks import workflows as files_jobs
    from keystone.platform.flow.tasks import workflows as flow_jobs
    from keystone.platform.model_access.tasks import workflows as model_jobs

    bindings, installers = [], []
    for module in (files_jobs, flow_jobs, model_jobs):
        tasks, install = module.register(runtime, health)
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
    )
    queues = queue_budgets(
        bindings,
        definitions,
        (files_jobs.CLEANUP_SCHEDULE, flow_jobs.RECONCILE_SCHEDULE, model_jobs.COST_SCHEDULE),
        other_entrypoints=(
            definition.entrypoint for definition in runtime.flow_definitions.values()
        )
        if runtime.settings.flow_enabled
        else (),
    )
    return queues, installers


def build_catalog():
    return merge_catalogs(PLATFORM_CATALOG, BUSINESS_CATALOGS)


def build_model_clients(settings, database):
    from keystone.platform.model_access.billing.registry import BillingClient
    from keystone.platform.model_access.clients import ModelClients
    from keystone.platform.model_access.coordination import QueryCoordination
    from keystone.platform.model_access.credentials import ChannelCredentials
    from keystone.platform.model_access.http import HttpClient
    from keystone.platform.model_access.inference.client import InferenceClient

    http = HttpClient()
    return ModelClients(
        http,
        InferenceClient(http),
        BillingClient(http),
        ChannelCredentials(settings.file_coordination_root),
        QueryCoordination(database, settings),
    )
