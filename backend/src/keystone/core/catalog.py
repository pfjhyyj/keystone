"""Immutable declarations shared by one application's database sessions."""

from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from types import MappingProxyType


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
