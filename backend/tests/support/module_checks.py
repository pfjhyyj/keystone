"""Static module boundaries shared by platform and future business modules."""

import ast

from support.ast_support import imports

LAYERS = {"controllers", "schemas", "models", "services", "adapters", "tasks", "workflows"}
MODEL_ACCESS = "keystone.platform.model_access"
CHANNELS = MODEL_ACCESS + ".channels"


def _under(dependency, prefix):
    return dependency == prefix or dependency.startswith(prefix + ".")


def _domain_layer(dependency, layer):
    parts = dependency.split(".")
    return (
        len(parts) >= 4
        and parts[:2]
        in (
            ["keystone", "platform"],
            ["keystone", "businesses"],
        )
        and parts[3] == layer
    )


def _module_dependencies(dependencies, namespace, file):
    for dependency in dependencies:
        if namespace.startswith("keystone.platform."):
            assert not _under(dependency, "keystone.businesses"), (
                f"Platform imports concrete business: {file}"
            )
        parts = dependency.split(".")
        if len(parts) >= 4 and parts[:2] in (
            ["keystone", "platform"],
            ["keystone", "businesses"],
        ):
            owner = ".".join(parts[:3])
            assert owner == namespace or not (
                parts[3] in {"models", "adapters"}
                or owner == MODEL_ACCESS
                and parts[3] == "channels"
            ), f"Module imports another module's persistence/adapter: {file} -> {dependency}"


def check_core_dependencies(root):
    for file in root.rglob("*.py"):
        package = ".".join(("keystone.core", *file.relative_to(root).parts[:-1]))
        dependencies = imports(ast.parse(file.read_text(encoding="utf8")), package)
        assert not any(
            _under(dependency, prefix)
            for dependency in dependencies
            for prefix in (
                "keystone.platform",
                "keystone.businesses",
                "keystone.composition",
                "keystone.bootstrap",
                "keystone.entrypoints",
                "keystone.model_registry",
                "keystone.use_cases",
                "keystone.worker",
            )
        ), f"Core imports domain/application implementation: {file}"


def _execution_dependencies(tree, package, *, allow_client=False, allow_worker_config_query=False):
    """Evaluate exceptions per import, never grant them to another statement."""
    execution = "keystone.core.execution"
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        dependencies = list(imports(node, package))
        base = dependencies[0]
        children = dependencies[1:] if isinstance(node, ast.ImportFrom) else []
        client_only = (
            allow_client
            and isinstance(node, ast.ImportFrom)
            and base == "dbos"
            and all(alias.name in {"DBOSClient", "error"} for alias in node.names)
        )
        worker_config_query_only = (
            allow_worker_config_query
            and base == "keystone.worker.registration"
            and children == ["keystone.worker.registration.shared_config_matches"]
        )
        contracts_only = bool(children) and all(
            _under(d, execution + ".contracts") for d in children
        )
        definitions_only = bool(children) and all(
            _domain_layer(d, "tasks") and len(d.split(".")) > 4 and d.split(".")[4] == "definitions"
            for d in children
        )
        for dependency in dependencies:
            if _under(dependency, "dbos") and not client_only:
                yield dependency
            if _under(dependency, "keystone.worker") and not worker_config_query_only:
                yield dependency
            if _under(dependency, execution) and not (
                _under(dependency, execution + ".contracts")
                or dependency == base == execution
                and contracts_only
            ):
                yield dependency
            if _domain_layer(dependency, "workflows"):
                yield dependency
            if _domain_layer(dependency, "tasks") and not (
                len(dependency.split(".")) > 4
                and dependency.split(".")[4] == "definitions"
                or dependency == base
                and definitions_only
            ):
                yield dependency


def _lightweight_root(file, tree, dependencies):
    allowed_import = "keystone.core.catalog" if file.name == "catalog.py" else None
    assert not any(
        d != allowed_import and d != f"{allowed_import}.Catalog" for d in dependencies
    ), f"Module root must contain lightweight declarations only: {file}"
    for node in tree.body:
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # Module docstring.
        assert isinstance(node, (ast.Assign, ast.AnnAssign)), (
            f"Module root must contain lightweight declarations only: {file}"
        )
        assert not any(
            isinstance(n, (ast.Await, ast.Lambda, ast.NamedExpr)) for n in ast.walk(node)
        ), f"Module root must contain lightweight declarations only: {file}"
        assert all(
            isinstance(n.func, ast.Name) and n.func.id in {"Catalog", "set", "frozenset"}
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
        ), f"Module root must contain lightweight declarations only: {file}"


def _channel_dependencies(tree, package, relative, file):
    registry = relative.as_posix() == "channels/__init__.py"
    owner = relative.parts[1] if len(relative.parts) > 2 else None
    composition = registry or (
        len(relative.parts) == 3 and owner != "shared" and file.name in {"__init__.py", "spec.py"}
    )
    billing = "billing" in relative.with_suffix("").parts[2:]
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        dependencies = list(imports(node, package))
        # ImportFrom's base is also emitted by imports(); inspect its children so
        # `from shared import billing` is not mistaken for a protocol dependency.
        if isinstance(node, ast.ImportFrom):
            dependencies = dependencies[1:]
        for dependency in dependencies:
            if not _under(dependency, CHANNELS):
                continue
            target = dependency.split(".")[4:]
            assert registry or target and target[0] in {owner, "shared", "spec", "rules"}, (
                f"Channel imports another channel's implementation/registry: {file} -> {dependency}"
            )
            if (
                composition
                or target[0] in {"spec", "rules"}
                or target[:2] == ["shared", "receipts"]
            ):
                continue
            target_billing = len(target) > 1 and target[1] == "billing"
            assert billing == target_billing, (
                f"Channel billing/protocol dependency crosses boundary: {file} -> {dependency}"
            )
            assert (
                target[0] == "shared"
                or len(target) > 1
                and target[1] not in {"SPEC", "spec", "__init__"}
            ), f"Channel implementation imports SPEC composition: {file} -> {dependency}"


def check_module_layers(root, namespace):
    layers = LAYERS | ({"channels"} if namespace == MODEL_ACCESS else set())
    for directory in root.iterdir():
        if directory.is_dir() and directory.name != "__pycache__":
            assert directory.name in layers, f"Unknown module layer: {directory}"
            assert (directory / "__init__.py").is_file(), f"Missing layer package: {directory}"
    for file in root.rglob("*.py"):
        relative = file.relative_to(root)
        package = ".".join((namespace, *relative.parts[:-1]))
        tree = ast.parse(file.read_text(encoding="utf8"))
        dependencies = list(imports(tree, package))
        if len(relative.parts) == 1:
            assert file.name in {"__init__.py", "catalog.py"}, (
                f"Implementation must live inside a module layer: {file}"
            )
            _lightweight_root(file, tree, dependencies)
            continue
        layer = relative.parts[0]
        _module_dependencies(dependencies, namespace, file)
        assert not any(
            _under(d, root)
            for d in dependencies
            for root in (
                "keystone.composition",
                "keystone.bootstrap",
                "keystone.entrypoints",
                "keystone.model_registry",
                "keystone.use_cases",
            )
        ), f"Module imports application assembly: {file}"
        if layer in {"controllers", "services", "schemas", "models", "adapters", "channels"}:
            forbidden = list(
                _execution_dependencies(
                    tree,
                    package,
                    allow_client=namespace == "keystone.platform.flow"
                    and relative.as_posix() == "adapters/dbos.py",
                    allow_worker_config_query=namespace == MODEL_ACCESS
                    and relative.as_posix() == "adapters/coordination.py",
                )
            )
            assert not forbidden, f"Module layer imports execution implementation: {file}"
        if layer == "controllers":
            assert not any(
                d.startswith("sqlalchemy")
                or "models" in d.split(".")
                or _under(d, "keystone.core.database")
                for d in dependencies
            ), f"Controller imports persistence: {file}"
            assert not any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"access", "transaction", "scalars", "scalar"}
                for n in ast.walk(tree)
            ), f"Controller executes persistence or authorization use case: {file}"
        if layer != "controllers":
            assert not any(
                d.startswith(("fastapi", "starlette"))
                or _under(d, "keystone.core.http")
                or _domain_layer(d, "controllers")
                for d in dependencies
            ), f"Module implementation imports HTTP/controller boundary: {file}"
        if layer in {"models", "schemas", "adapters", "channels"}:
            assert not any(_domain_layer(d, "services") for d in dependencies), (
                f"Lower layer imports business Service: {file}"
            )
        if layer in {"schemas", "adapters", "channels"}:
            assert not any(_domain_layer(d, "models") for d in dependencies), (
                f"Schema/adapter imports domain persistence: {file}"
            )
        if layer == "schemas":
            assert not any(
                d.startswith("sqlalchemy")
                or _under(d, "keystone.core.database")
                or _under(d, "keystone.worker.models")
                or _domain_layer(d, "adapters")
                for d in dependencies
            ), f"Schema imports runtime infrastructure: {file}"
            assert all(
                not _under(d, CHANNELS)
                or namespace == MODEL_ACCESS
                and (
                    d == CHANNELS
                    or d
                    in {
                        f"{CHANNELS}.{name}"
                        for name in ("CHANNELS", "INSTALLED", "REFERENCE_PRICES")
                    }
                    or _under(d, CHANNELS + ".spec")
                    or _under(d, CHANNELS + ".rules")
                )
                for d in dependencies
            ), f"Schema imports channel implementation: {file}"
        if layer == "channels":
            assert not any(
                d.startswith("sqlalchemy")
                or any(_domain_layer(d, layer) for layer in ("adapters", "tasks", "workflows"))
                or _under(d, "keystone.platform.identity")
                or any(
                    _under(d, prefix)
                    for prefix in (
                        "keystone.core.errors",
                        "keystone.core.database",
                        "keystone.worker.models",
                        "keystone.core.execution",
                    )
                )
                for d in dependencies
            ), f"Channel imports runtime infrastructure: {file}"
            _channel_dependencies(tree, package, relative, file)
        if layer == "models":
            assert not any(
                _domain_layer(d, "adapters") or _under(d, CHANNELS) for d in dependencies
            ), f"Model imports runtime adapter: {file}"
        if layer == "services":
            assert not any(
                d.startswith(("fastapi", "starlette", "openai", "httpx2", "casbin"))
                for d in dependencies
            ), f"Service imports protocol SDK: {file}"
        if layer in {"tasks", "workflows"}:
            assert not any(
                d.startswith("sqlalchemy")
                or _domain_layer(d, "models")
                or _under(d, "keystone.core.database")
                or _under(d, "keystone.worker.models")
                for d in dependencies
            ), f"Execution adapter owns domain persistence: {file}"
        if layer == "tasks":
            assert not any(
                d.startswith("sqlalchemy")
                or "models" in d.split(".")
                or d.startswith("keystone.core.database")
                for d in dependencies
            ), f"Task adapter owns persistence: {file}"
            if file.name == "definitions.py":
                assert not list(_execution_dependencies(tree, package)) and not any(
                    _domain_layer(d, "services")
                    or _domain_layer(d, "adapters")
                    or _under(d, CHANNELS)
                    or d.startswith(("openai", "httpx2", "casbin", "pydantic_ai"))
                    for d in dependencies
                ), f"Task declaration imports execution implementation: {file}"
