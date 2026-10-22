"""Immutable declarations and validation shared by one application's database sessions."""

import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from types import MappingProxyType

NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,47}")
FIELD = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class Catalog:
    permissions: Mapping[str, str] = field(default_factory=dict)
    audit_actions: Mapping[str, Set[str]] = field(default_factory=dict)

    def __post_init__(self):
        if any(isinstance(fields, (str, bytes)) for fields in self.audit_actions.values()):
            raise ValueError("Audit fields must be a collection, not a string")
        object.__setattr__(self, "permissions", MappingProxyType(dict(self.permissions)))
        object.__setattr__(
            self,
            "audit_actions",
            MappingProxyType(
                {key: frozenset(fields) for key, fields in self.audit_actions.items()}
            ),
        )


def combine_catalogs(catalogs: Sequence[Catalog]) -> Catalog:
    """Combine explicit platform declarations while rejecting accidental overrides."""
    permissions, actions = {}, {}
    for catalog in catalogs:
        if (
            permissions.keys() & catalog.permissions.keys()
            or actions.keys() & catalog.audit_actions.keys()
        ):
            raise ValueError("Duplicate platform permission or audit action")
        permissions.update(catalog.permissions)
        actions.update(catalog.audit_actions)
    return Catalog(permissions, actions)


def merge_catalogs(platform: Catalog, businesses: Sequence[tuple[str, Catalog]]) -> Catalog:
    """Merge validated business declarations without allowing platform overrides."""
    permissions, actions = dict(platform.permissions), dict(platform.audit_actions)
    reserved = {key.split(":")[0] for key in permissions} | {key.split(".")[0] for key in actions}
    seen = set()
    for namespace, catalog in businesses:
        permission_pattern = re.compile(
            rf"{namespace}\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*:[a-z][a-z0-9_]*"
        )
        audit_pattern = re.compile(rf"{namespace}\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
        if not NAMESPACE.fullmatch(namespace) or namespace in reserved | seen:
            raise ValueError("Invalid or duplicate business namespace")
        seen.add(namespace)
        for key, description in catalog.permissions.items():
            if (
                not permission_pattern.fullmatch(key)
                or len(key) > 100
                or not description.strip()
                or key in permissions
            ):
                raise ValueError("Invalid or duplicate business permission")
            permissions[key] = description
        for key, fields in catalog.audit_actions.items():
            if (
                not audit_pattern.fullmatch(key)
                or len(key) > 100
                or key in actions
                or any(not FIELD.fullmatch(name) for name in fields)
            ):
                raise ValueError("Invalid or duplicate business audit action")
            actions[key] = fields
    return Catalog(permissions, actions)
