"""Static module boundaries shared by platform and future business modules."""

import ast

from support.ast_support import imports


def _under(dependency, prefix):
    return dependency == prefix or dependency.startswith(prefix + ".")


def check_module_layers(root, namespace):
    for file in root.rglob("*.py"):
        relative = file.relative_to(root)
        package = ".".join((namespace, *relative.parts[:-1]))
        tree = ast.parse(file.read_text(encoding="utf8"))
        dependencies = list(imports(tree, package))
        layer = relative.parts[0]
        if layer in {"controllers", "services", "schemas", "models", "policies", "adapters"}:
            forbidden = [d for d in dependencies if d.startswith(("dbos", "keystone.worker"))]
            execution = "keystone.core.execution"
            forbidden.extend(
                d
                for d in dependencies
                if (d.startswith(execution + ".") and not _under(d, execution + ".contracts"))
            )
            if execution in dependencies and not any(
                _under(d, execution + ".contracts") for d in dependencies
            ):
                forbidden.append(execution)
            forbidden.extend(
                d
                for d in dependencies
                if (".tasks." in d and ".tasks.definitions" not in d or "workflows" in d.split("."))
            )
            assert not forbidden, f"Module layer imports execution implementation: {file}"
        if layer == "controllers":
            assert not any(
                d.startswith("sqlalchemy") or "models" in d.split(".") for d in dependencies
            ), f"Controller imports persistence: {file}"
        if layer == "tasks":
            assert not any(
                d.startswith("sqlalchemy")
                or "models" in d.split(".")
                or d.startswith("keystone.core.database")
                for d in dependencies
            ), f"Task adapter owns persistence: {file}"
            if file.name == "definitions.py":
                assert not any(
                    d.startswith("dbos")
                    or "services" in d.split(".")
                    or "workflows" in d.split(".")
                    or (
                        d.startswith("keystone.core.execution.")
                        and not _under(d, "keystone.core.execution.contracts")
                    )
                    for d in dependencies
                ), f"Task declaration imports execution implementation: {file}"
