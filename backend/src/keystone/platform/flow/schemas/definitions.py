"""Lightweight code contracts. Importing this directory never registers an executor."""

from dataclasses import dataclass
from types import MappingProxyType

from pydantic import BaseModel

from keystone.platform.flow.schemas.files import validate_file_paths


@dataclass(frozen=True)
class Definition:
    key: str
    name: str
    description: str
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    entrypoint: str
    required_permissions: tuple[str, ...] = ()
    file_fields: tuple[str, ...] = ()
    output_file_fields: tuple[str, ...] = ()
    # Trusted code declaration, not a new persisted field or public import path.
    model_field: str | None = None
    timeout_seconds: int = 300

    def output_schema(self):
        # Match workflow model_dump(mode="json", by_alias=False), including serializers.
        return self.output_type.model_json_schema(mode="serialization", by_alias=False)

    def view(self):
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_type.model_json_schema(),
            "output_schema": self.output_schema(),
            "entrypoint": self.entrypoint,
            "required_permissions": self.required_permissions,
            "file_fields": self.file_fields,
            "output_file_fields": self.output_file_fields,
            "timeout_seconds": self.timeout_seconds,
        }

    def snapshot(self):
        return {
            "output_schema": self.output_schema(),
            "output_file_fields": list(self.output_file_fields),
        }


def build_definitions(definitions, permissions):
    """Validate the complete explicit list and freeze this application's directory."""
    result, entrypoints = {}, set()
    for definition in definitions:
        if (
            type(definition.timeout_seconds) is not int
            or not 1 <= definition.timeout_seconds <= 86400
        ):
            raise ValueError("Flow timeout must be between 1 and 86400 seconds")
        if not definition.key or definition.key in result:
            raise ValueError("Empty or duplicate Flow definition key")
        if set(definition.required_permissions) - permissions.keys():
            raise ValueError("Flow requires undeclared permissions")
        if not definition.entrypoint.strip() or definition.entrypoint in entrypoints:
            raise ValueError("Empty or duplicate Flow entrypoint")
        input_schema = definition.input_type.model_json_schema(mode="serialization", by_alias=False)
        if definition.model_field is not None and definition.model_field not in input_schema.get(
            "properties", {}
        ):
            raise ValueError("Flow model field must be declared in its input")
        validate_file_paths(input_schema, definition.file_fields)
        validate_file_paths(definition.output_schema(), definition.output_file_fields)
        result[definition.key] = definition
        entrypoints.add(definition.entrypoint)
    return MappingProxyType(result)
