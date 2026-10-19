"""Explicit code declarations; no discovery, global registration or business table."""

import re

from keystone.core.catalog import Catalog
from keystone.platform.audit.services.writer import ACTIONS
from keystone.platform.identity.policy import PERMISSIONS

# Import each business's lightweight CATALOG here and add (namespace, CATALOG).
BUSINESS_CATALOGS: tuple[tuple[str, Catalog], ...] = ()


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
