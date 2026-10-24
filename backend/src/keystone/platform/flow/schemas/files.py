"""Schema-checked, bounded file identifier traversal; no I/O or authorization."""

import re
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CompiledFilePath:
    parts: tuple[str, ...]


def compile_file_paths(schema, paths):
    if len(paths) > 32 or len(paths) != len(set(paths)):
        raise ValueError("Invalid Flow file paths")

    def declared(node, parts, seen=frozenset(), depth=0):
        if depth > 64:
            raise ValueError("Recursive Flow file paths")
        if "$ref" in node:
            ref = node["$ref"]
            marker = (ref, parts)
            if not ref.startswith("#/") or marker in seen:
                raise ValueError("Unsupported Flow file paths reference")
            target = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return declared(target, parts, seen | {marker}, depth + 1)
        variants = node.get("anyOf", node.get("oneOf"))
        if variants is not None:
            variants = [v for v in variants if v.get("type") != "null"]
            plans = [declared(v, parts, seen, depth + 1) for v in variants]
            if not plans or any(p != plans[0] for p in plans):
                raise ValueError("Ambiguous Flow file paths")
            return plans[0]
        if parts:
            head, *rest = parts
            if head == "*":
                if node.get("type") != "array":
                    raise ValueError("Flow file paths wildcard requires an array")
                return ("*", *declared(node["items"], tuple(rest), seen, depth + 1))
            child = node.get("properties", {}).get(head)
            if child is None:
                raise ValueError("Flow file paths field is not declared")
            return (head, *declared(child, tuple(rest), seen, depth + 1))
        if node.get("type") == "string":
            return ()
        if node.get("type") == "array":
            terminal = declared(node.get("items", {}), (), seen, depth + 1)
            if terminal:
                raise ValueError("Flow file paths terminal list must contain IDs")
            return ("*",)
        raise ValueError("Flow file paths must identify strings or ID lists")

    compiled = []
    for path in paths:
        if (
            not isinstance(path, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|\*))*", path)
            or len(path.split(".")) > 8
        ):
            raise ValueError("Invalid Flow file paths")
        try:
            parts = declared(schema, tuple(path.split(".")))
        except (AttributeError, KeyError, TypeError, RecursionError):
            raise ValueError("Invalid Flow file paths schema") from None
        if any(p.parts == parts for p in compiled):
            raise ValueError("Overlapping Flow file paths")
        compiled.append(CompiledFilePath(parts))
    return tuple(compiled)


def extract_file_ids(value, compiled_paths):
    result, visits = set(), 0

    def visit(current, parts):
        nonlocal visits
        visits += 1
        if visits > 4096:
            raise ValueError("Flow file traversal exceeds limit")
        if current is None:
            return
        if not parts:
            if not isinstance(current, (str, UUID)):
                raise ValueError("Invalid file ID")
            result.add(UUID(str(current)))
            if len(result) > 100:
                raise ValueError("Too many files")
            return
        head, *rest = parts
        if head == "*":
            if not isinstance(current, list):
                raise ValueError("Expected file collection")
            for item in current:
                visit(item, rest)
        else:
            if not isinstance(current, dict) or head not in current:
                raise ValueError("Missing file field")
            visit(current[head], rest)

    for path in compiled_paths:
        visit(value, path.parts)
    return result


def validate_legacy_file_paths(schema, paths):
    """Read-only compatibility for pre-format snapshots; no new path/union limits."""

    def declared(node, parts, *, collection=True, seen=frozenset()):
        if "$ref" in node:
            ref = node["$ref"]
            marker = (ref, parts, collection)
            if not ref.startswith("#/") or marker in seen:
                return False
            target = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return declared(target, parts, collection=collection, seen=seen | {marker})
        if "anyOf" in node:
            variants = [item for item in node["anyOf"] if item.get("type") != "null"]
            return bool(variants) and all(
                declared(item, parts, collection=collection, seen=seen) for item in variants
            )
        if parts:
            child = node.get("properties", {}).get(parts[0])
            return child is not None and declared(child, parts[1:], seen=seen)
        return node.get("type") == "string" or (
            collection
            and node.get("type") == "array"
            and declared(node.get("items", {}), (), collection=False, seen=seen)
        )

    try:
        if len(paths) != len(set(paths)) or any(
            not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", path)
            or not declared(schema, tuple(path.split(".")))
            for path in paths
        ):
            raise ValueError("Invalid legacy Flow file paths")
    except (AttributeError, KeyError, TypeError, RecursionError):
        raise ValueError("Invalid legacy Flow file paths schema") from None


def extract_legacy_file_ids(value, paths):
    """Keep dot traversal and runtime ID-or-list terminals for persisted v1 results."""
    result = set()
    for path in paths:
        current = value
        for part in path.split("."):
            if current is None:
                break
            if not isinstance(current, dict) or part not in current:
                raise ValueError("Missing legacy file field")
            current = current[part]
        values = current if isinstance(current, list) else [current]
        for identifier in values:
            if identifier is not None:
                result.add(UUID(str(identifier)))
                if len(result) > 100:
                    raise ValueError("Too many files")
    return result
