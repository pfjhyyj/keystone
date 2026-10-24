"""Explicit package manifests, independent of ORM and execution imports."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from keystone.platform.flow.schemas.definitions import FlowDefinition, build_definitions


@dataclass(frozen=True)
class PackageResource:
    package: str
    path: str

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z_]\w*", self.package):
            raise ValueError("Resource must name a top-level Python package")
        path = PurePosixPath(self.path)
        if not self.path or path.is_absolute() or ".." in path.parts or "\\" in self.path:
            raise ValueError("Resource must be a package-relative file")
        object.__setattr__(self, "path", path.as_posix())


@dataclass(frozen=True)
class FlowPackage:
    key: str
    host_api_version: int
    definitions: tuple[FlowDefinition, ...]
    execution_modules: tuple[str, ...]
    execution_packages: tuple[str, ...] = ()
    required_resources: tuple[PackageResource, ...] = ()

    def __post_init__(self):
        for name in (
            "definitions",
            "execution_modules",
            "execution_packages",
            "required_resources",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if (
            not self.key.strip()
            or type(self.host_api_version) is not int
            or self.host_api_version != 1
        ):
            raise ValueError("Invalid Flow package or incompatible host API version")
        for name in self.execution_packages:
            if not re.fullmatch(r"[A-Za-z_]\w*", name) or name == "keystone":
                raise ValueError("Invalid external execution package")
        allowed = {"keystone", *self.execution_packages}
        for module in self.execution_modules:
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", module):
                raise ValueError("Invalid execution module")
            if module.split(".")[0] not in allowed:
                raise ValueError("Execution module requires an explicit package")
        if any(r.package not in allowed for r in self.required_resources):
            raise ValueError("Resource requires an explicit execution package")


def build_packages(packages, permissions):
    packages = tuple(packages)
    if len({p.key for p in packages}) != len(packages):
        raise ValueError("Duplicate Flow package key")
    definitions = build_definitions((d for p in packages for d in p.definitions), permissions)
    owners = MappingProxyType({d.key: p.key for p in packages for d in p.definitions})
    return definitions, owners


def execution_manifest(packages):
    resources = {}
    declarations = []
    for package in packages:
        for name in package.execution_packages:
            resources.setdefault(name, set())
        for resource in package.required_resources:
            resources.setdefault(resource.package, set()).add(resource.path)
        declarations.append(
            {
                "key": package.key,
                "host_api_version": package.host_api_version,
                "definitions": [d.view() for d in package.definitions],
                "execution_modules": sorted(set(package.execution_modules)),
                "execution_packages": sorted(set(package.execution_packages)),
                "required_resources": sorted(
                    (r.package, r.path) for r in package.required_resources
                ),
            }
        )
    return {k: tuple(sorted(v)) for k, v in resources.items()}, sorted(
        declarations, key=lambda p: p["key"]
    )
