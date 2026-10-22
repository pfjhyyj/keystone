"""Test-only discovery verifies explicit production ORM registration."""

import ast
from importlib import import_module

from support.ast_support import imports


def check_business_models(root):
    from keystone.core.database import Base
    from keystone.model_registry import load_models

    metadata = load_models()
    # Capture before importing discovered files: discovery must not hide a missing registration.
    registered = {mapper.class_ for mapper in Base.registry.mappers}
    for file in sorted((root / "businesses").glob("*/models/**/*.py")):
        module_name = ".".join(("keystone", *file.relative_to(root).with_suffix("").parts))
        package = module_name.rsplit(".", 1)[0]
        tree = ast.parse(file.read_text(encoding="utf-8"))
        dependencies = list(imports(tree, package))
        assert not any(
            dep.startswith(("fastapi", "starlette", "dbos"))
            or any(
                part in {"controllers", "services", "workflow", "workflows", "agents"}
                for part in dep.split(".")
            )
            for dep in dependencies
        ), f"Model dependency crosses layers: {file}"
        if file.name == "__init__.py":
            continue
        module = import_module(module_name)
        entities = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, Base)
            and value.__module__ == module_name
        ]
        assert len(entities) == 1, f"Expected one entity per file: {file}"
        entity = entities[0]
        assert entity in registered, f"Model missing from load_models: {module_name}"
        business = file.relative_to(root).parts[1]
        exports = import_module(f"keystone.businesses.{business}.models")
        assert getattr(exports, entity.__name__, None) is entity, (
            f"Model not exported: {module_name}"
        )
        table = entity.__table__
        assert table.metadata is metadata and table.schema == "keystone", (
            f"Model must share keystone metadata/schema: {module_name}"
        )
        assert table.name.startswith(f"{business}_"), f"Model table prefix mismatch: {module_name}"
