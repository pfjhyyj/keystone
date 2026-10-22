"""Pure validation and extraction of declared Flow file identifiers."""

import re
from uuid import UUID


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
