"""Explicit code declarations; no discovery, global registration or business table."""

import re

from keystone.core.catalog import Catalog
from keystone.platform.audit.services.writer import ACTIONS
from keystone.platform.flow.catalog import build_definitions
from keystone.platform.identity.policy import PERMISSIONS

# Import each business's lightweight CATALOG here and add (namespace, CATALOG).
BUSINESS_CATALOGS: tuple[tuple[str, Catalog], ...] = ()
FLOW_DEFINITIONS = ()
# Package import name -> required resource paths. API and Worker hash the same files.
EXECUTION_PACKAGES: dict[str, tuple[str, ...]] = {}


def build_flow_definitions(catalog):
    return build_definitions(FLOW_DEFINITIONS, catalog.permissions)


def register_workflows():
    # Application composition adds Worker-only imports here.
    pass


def register_maintenance(runtime, health):
    # Always register guarded entrypoints: persisted ticks may arrive before a
    # disabled plan is paused. No runtime handles or credentials enter inputs.
    from keystone.worker.tasks import files as files_jobs
    from keystone.worker.tasks import flow as flow_jobs
    from keystone.worker.tasks import model_costs as model_jobs

    installers = (
        files_jobs.register(runtime, health),
        flow_jobs.register(runtime, health),
        model_jobs.register(runtime, health),
    )
    queues = {
        model_jobs.QUEUE: (1, 1),
        model_jobs.QUERY_QUEUE: (
            runtime.settings.model_cost_concurrency,
            runtime.settings.model_cost_concurrency,
        ),
    }
    if runtime.settings.file_storages:
        queues[files_jobs.QUEUE] = (1, 1)
    if runtime.settings.flow_enabled:
        queues[flow_jobs.QUEUE] = (1, 1)
    return queues, installers


def build_catalog():
    permissions, actions = dict(PERMISSIONS), dict(ACTIONS)
    reserved = {key.split(":")[0] for key in permissions} | {key.split(".")[0] for key in actions}
    seen = set()
    for namespace, catalog in BUSINESS_CATALOGS:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", namespace) or namespace in reserved | seen:
            raise ValueError("Invalid or duplicate business namespace")
        seen.add(namespace)
        for key, description in catalog.permissions.items():
            if (
                not re.fullmatch(
                    rf"{namespace}\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*:[a-z][a-z0-9_]*", key
                )
                or len(key) > 100
                or not description.strip()
                or key in permissions
            ):
                raise ValueError("Invalid or duplicate business permission")
            permissions[key] = description
        for key, fields in catalog.audit_actions.items():
            if (
                not re.fullmatch(rf"{namespace}\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", key)
                or len(key) > 100
                or key in actions
                or any(not re.fullmatch(r"[a-z][a-z0-9_]*", field) for field in fields)
            ):
                raise ValueError("Invalid or duplicate business audit action")
            actions[key] = fields
    return Catalog(permissions, actions)


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
