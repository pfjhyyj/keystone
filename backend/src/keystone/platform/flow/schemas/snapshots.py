"""Persisted execution contract for the current snapshot format."""

from typing import Literal

from pydantic import Field, ValidationError, model_validator

from keystone.core.errors import ServiceError
from keystone.core.schemas import Input
from keystone.platform.flow.schemas.files import compile_file_paths
from keystone.platform.flow.schemas.values import small_json
from keystone.platform.model_access.schemas.invocations import ResolvedModelBinding
from keystone.platform.model_access.schemas.operations import Delivery, Operation


class RoleSnapshot(Input):
    operation: Operation
    delivery: Delivery = "sync"


class Snapshot(Input):
    format_version: Literal[3]
    package_key: str = Field(min_length=1)
    host_api_version: int = Field(strict=True, ge=1, le=1)
    revision: str = Field(min_length=1, max_length=64)
    entrypoint: str = Field(min_length=1)
    required_permissions: list[str]
    timeout_seconds: int = Field(strict=True, ge=1, le=86400)
    input_schema: dict
    output_schema: dict
    file_fields: list[str]
    output_file_fields: list[str]
    model_roles: dict[str, RoleSnapshot]
    model_bindings: dict[str, ResolvedModelBinding]

    @model_validator(mode="after")
    def coherent(self):
        if len(self.model_roles) > 16 or self.model_roles.keys() != self.model_bindings.keys():
            raise ValueError("Snapshot model roles do not match bindings")
        if any(
            b.selection.operation != self.model_roles[role].operation
            or b.selection.delivery != self.model_roles[role].delivery
            for role, b in self.model_bindings.items()
        ):
            raise ValueError("Snapshot operation does not match role")
        compile_file_paths(self.input_schema, self.file_fields)
        compile_file_paths(self.output_schema, self.output_file_fields)
        return self


def read_snapshot(value):
    try:
        small_json(value)
        data = Snapshot.model_validate(value)
        return data.model_dump(mode="json")
    except (ValidationError, ValueError, KeyError, TypeError, ServiceError):
        raise ServiceError(503, "flow_snapshot_unavailable", "运行契约不可用") from None


def public_snapshot(value):
    snapshot = read_snapshot(value)
    return {
        key: snapshot[key]
        for key in (
            "format_version",
            "revision",
            "host_api_version",
            "input_schema",
            "output_schema",
            "file_fields",
            "output_file_fields",
            "model_roles",
            "timeout_seconds",
        )
    }


def matches_definition(snapshot, definition):
    return all(
        (
            snapshot["revision"] == definition.revision,
            snapshot["entrypoint"] == definition.entrypoint,
            snapshot["required_permissions"] == list(definition.required_permissions),
            snapshot["timeout_seconds"] == definition.timeout_seconds,
            snapshot["input_schema"] == definition.input_schema(),
            snapshot["output_schema"] == definition.output_schema(),
            snapshot["file_fields"] == list(definition.file_fields),
            snapshot["output_file_fields"] == list(definition.output_file_fields),
            snapshot["model_roles"]
            == {
                r.key: {"operation": r.operation, "delivery": r.delivery}
                for r in definition.model_roles
            },
        )
    )
