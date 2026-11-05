"""Lightweight, immutable Flow contracts; importing never registers an executor."""

import re
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import BaseModel

from keystone.platform.flow.schemas.files import compile_file_paths
from keystone.platform.model_access.schemas.operations import OPERATIONS, Delivery, Operation


@dataclass(frozen=True)
class ModelRole:
    key: str
    label: str
    operation: Operation = "text.generate"
    delivery: Delivery = "sync"

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.key):
            raise ValueError("Invalid model role key")
        if not 1 <= len(self.label.strip()) <= 128 or self.operation not in OPERATIONS:
            raise ValueError("Invalid model role label or operation")
        if (
            self.delivery not in {"sync", "async"}
            or self.delivery == "async"
            and self.operation != "image.generate"
        ):
            raise ValueError("Unsupported model delivery")
        object.__setattr__(self, "label", self.label.strip())


@dataclass(frozen=True)
class FlowDefinition:
    key: str
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    entrypoint: str
    revision: str
    required_permissions: tuple[str, ...] = ()
    file_fields: tuple[str, ...] = ()
    output_file_fields: tuple[str, ...] = ()
    model_roles: tuple[ModelRole, ...] = ()

    timeout_seconds: int | None = 300
    queue: str = "keystone-flows"

    def __post_init__(self):
        if (
            not isinstance(self.queue, str)
            or not self.queue.strip()
            or self.queue != self.queue.strip()
        ):
            raise ValueError("Invalid Flow queue")
        for name in ("required_permissions", "file_fields", "output_file_fields", "model_roles"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not self.revision.strip() or len(self.revision) > 64:
            raise ValueError("Invalid Flow revision")
        if self.timeout_seconds is not None and (
            type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 86400
        ):
            raise ValueError("Flow timeout must be between 1 and 86400 seconds")
        for model in (self.input_model, self.output_model):
            if (
                not isinstance(model, type)
                or not issubclass(model, BaseModel)
                or getattr(model, "__pydantic_root_model__", False)
                or model.model_json_schema().get("type") != "object"
                or model.model_json_schema(mode="serialization", by_alias=False).get("type")
                != "object"
            ):
                raise ValueError("Flow contracts must be object models")
        if self.input_model.model_config.get("extra") != "forbid":
            raise ValueError("Flow input must forbid extra fields")
        if len(self.model_roles) > 16 or len({r.key for r in self.model_roles}) != len(
            self.model_roles
        ):
            raise ValueError("Duplicate or excessive model roles")
        if self.model_roles and "model:invoke" not in self.required_permissions:
            raise ValueError("Model roles require model:invoke")
        compile_file_paths(self.input_schema(), self.file_fields)
        compile_file_paths(self.output_schema(), self.output_file_fields)

    def input_schema(self):
        return self.input_model.model_json_schema(mode="serialization", by_alias=False)

    def output_schema(self):
        return self.output_model.model_json_schema(mode="serialization", by_alias=False)

    def view(self):
        return {
            "key": self.key,
            "revision": self.revision,
            "host_api_version": 1,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_schema(),
            "entrypoint": self.entrypoint,
            "required_permissions": self.required_permissions,
            "file_fields": self.file_fields,
            "output_file_fields": self.output_file_fields,
            "model_roles": [
                {"key": r.key, "label": r.label, "operation": r.operation, "delivery": r.delivery}
                for r in self.model_roles
            ],
            "timeout_seconds": self.timeout_seconds,
            "queue": self.queue,
        }

    def snapshot(self, *, package_key, model_bindings):
        return {
            "format_version": 3,
            "package_key": package_key,
            "host_api_version": 1,
            "revision": self.revision,
            "entrypoint": self.entrypoint,
            "required_permissions": list(self.required_permissions),
            "timeout_seconds": self.timeout_seconds,
            "queue": self.queue,
            "input_schema": self.input_schema(),
            "output_schema": self.output_schema(),
            "file_fields": list(self.file_fields),
            "output_file_fields": list(self.output_file_fields),
            "model_roles": {
                r.key: {"operation": r.operation, "delivery": r.delivery} for r in self.model_roles
            },
            "model_bindings": model_bindings,
        }


def build_definitions(definitions, permissions):
    result, entrypoints = {}, set()
    for definition in definitions:
        if not definition.key.strip() or len(definition.key) > 100 or definition.key in result:
            raise ValueError("Empty or duplicate Flow definition key")
        if len(set(definition.required_permissions)) != len(definition.required_permissions):
            raise ValueError("Duplicate Flow permissions")
        if set(definition.required_permissions) - permissions.keys():
            raise ValueError("Flow requires undeclared permissions")
        if not definition.entrypoint.strip() or definition.entrypoint in entrypoints:
            raise ValueError("Empty or duplicate Flow entrypoint")
        result[definition.key] = definition
        entrypoints.add(definition.entrypoint)
    return MappingProxyType(result)
