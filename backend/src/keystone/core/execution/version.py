"""Hash the loaded artifact and explicitly declared execution resources."""

import hashlib
import re
from importlib.util import find_spec
from pathlib import Path

from keystone.core.errors import ServiceError


def artifact_version(root: Path, packages):
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
    for name, required in sorted(packages.items()):
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
