"""Lightweight code contracts. Importing this directory never registers an executor."""

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

from pydantic import BaseModel

from keystone.core.errors import ServiceError


def small_json(value):
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(raw) > 65536:
            raise ValueError()
        return raw
    except (ValueError, TypeError, RecursionError):
        raise ServiceError(
            422, "flow_value_too_large", "流程数据须为不超过 64 KiB 的 JSON"
        ) from None


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


def validate_file_paths(schema, paths):
    """Check existing object paths and terminal ID lists, adapted from Ether."""

    def declared(node, parts, *, collection=True):
        if "$ref" in node:
            target = schema
            for part in node["$ref"].split("/")[1:]:
                target = target[part]
            return declared(target, parts, collection=collection)
        if "anyOf" in node:
            variants = [item for item in node["anyOf"] if item.get("type") != "null"]
            return bool(variants) and all(
                declared(item, parts, collection=collection) for item in variants
            )
        if parts:
            child = node.get("properties", {}).get(parts[0])
            return child is not None and declared(child, parts[1:])
        return node.get("type") == "string" or (
            collection
            and node.get("type") == "array"
            and declared(node.get("items", {}), [], collection=False)
        )

    if len(paths) != len(set(paths)) or any(
        not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", path)
        or not declared(schema, path.split("."))
        for path in paths
    ):
        raise ValueError("Flow file paths must identify declared string fields or lists")


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


@lru_cache
def execution_version():
    from keystone.composition import EXECUTION_PACKAGES

    root = Path(__file__).resolve().parents[2]
    # Hash the package actually loaded by this process, including wheel installations.
    # A stale deployment source copy must not hide a different installed executor.
    lock = next((p / "uv.lock" for p in root.parents if (p / "uv.lock").is_file()), None)
    if lock is None:
        raise ServiceError(503, "execution_version_unavailable", "执行制品缺少 uv.lock")
    digest = hashlib.sha256(lock.read_bytes())
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".md", ".json", ".txt"}:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    for name, required in sorted(EXECUTION_PACKAGES.items()):
        _package_version(digest, name, required)
    return digest.hexdigest()


def _package_version(digest, name, required):
    """Explicit top-level packages, including resources and native-process helpers.

    Required resources are deployment assertions, not a plugin discovery mechanism.
    Hashing every shipped file also covers resources added after this declaration.
    """
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise ValueError()
        spec = find_spec(name)
        if spec is None or not spec.origin or not spec.submodule_search_locations:
            raise ValueError()
        root = Path(spec.origin).resolve().parent
        if Path(spec.origin).name != "__init__.py" or not (root / "__init__.py").is_file():
            raise ValueError()
        for resource in required:
            path = (root / resource).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError()
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError()
            if not path.is_file():
                continue
            label = f"{name}/{path.relative_to(root).as_posix()}".encode()
            contents = hashlib.sha256()
            with path.open("rb") as stream:
                # Resources can be binary or large. Never load the whole package into memory.
                text = path.suffix in {".py", ".md", ".json", ".txt"}
                pending = b""
                while chunk := stream.read(1024 * 1024):
                    chunk = pending + chunk
                    pending = b"\r" if text and chunk.endswith(b"\r") else b""
                    if pending:
                        chunk = chunk[:-1]
                    contents.update(chunk.replace(b"\r\n", b"\n") if text else chunk)
                contents.update(pending)
            digest.update(len(label).to_bytes(4, "big"))
            digest.update(label)
            digest.update(contents.digest())
    except (ImportError, OSError, ValueError, TypeError):
        raise ServiceError(
            503, "execution_version_unavailable", "外部执行包或必需资源不可用"
        ) from None


def file_ids(value, paths):
    result = set()
    for path in paths:
        current = value
        for part in path.split("."):
            if current is None:
                break
            current = current[part]
        values = current if isinstance(current, list) else [current]
        result.update(UUID(str(item)) for item in values if item is not None)
    if len(result) > 100:
        raise ValueError("Too many files")
    return result
