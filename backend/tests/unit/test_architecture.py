"""Recursive architecture checks, including relative imports and internal adapters."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support.ast_support import imports
from support.module_checks import check_module_layers

ROOT = Path(__file__).parents[2] / "src" / "keystone"
DOMAINS = ("audit", "dictionary", "files", "flow", "identity", "model_access")
LAYERS = ("controllers", "services", "models", "schemas")


@pytest.mark.parametrize("domain", DOMAINS)
def test_platform_module_contract_and_dependencies(domain):
    root = ROOT / "platform" / domain
    for layer in LAYERS:
        assert (root / layer / "__init__.py").is_file()
        assert any(f.name != "__init__.py" for f in (root / layer).glob("*.py"))
    for old in ("controller", "service", "models", "schemas", "routes"):
        assert not (root / f"{old}.py").exists()

    for file in root.rglob("*.py"):
        relative = file.relative_to(ROOT.parent).with_suffix("")
        package = ".".join(relative.parts[:-1])
        tree = ast.parse(file.read_text(encoding="utf-8"))
        dependencies = list(imports(tree, package))
        layer = file.relative_to(root).parts[0]
        if layer in {"controllers", "services", "schemas", "models", "policies", "adapters"}:
            assert not any(d.startswith(("dbos", "keystone.worker")) for d in dependencies), file
        if layer == "controllers":
            assert not any(
                d.startswith("sqlalchemy") or ".models" in d.split("platform")[-1]
                for d in dependencies
            ), file
            assert not any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"access", "transaction", "scalars", "scalar"}
                for n in ast.walk(tree)
            ), file
        elif file.name != "dependencies.py":
            assert not any(
                d.startswith(("fastapi", "starlette"))
                or ".controllers" in d
                or d.endswith(".dependencies")
                for d in dependencies
            ), file
        if layer in {"schemas", "models"}:
            assert not any(".services" in d or d.endswith(".gateway") for d in dependencies), file
        if layer == "schemas":
            assert not any(
                d.startswith("sqlalchemy") or ".models" in d or d.endswith(".database")
                for d in dependencies
            ), file
            assert not any(
                isinstance(n, ast.Attribute) and n.attr == "__table__" for n in ast.walk(tree)
            ), file
        if layer == "models" and file.name != "__init__.py":
            entities = [n for n in tree.body if isinstance(n, ast.ClassDef)]
            assert len(entities) == 1, file
            assert any(isinstance(b, ast.Name) and b.id == "Base" for b in entities[0].bases)
        if layer == "services":
            assert not any(
                isinstance(n, ast.ClassDef)
                and any(isinstance(b, ast.Name) and b.id in {"Input", "BaseModel"} for b in n.bases)
                for n in ast.walk(tree)
            ), file
        if domain == "flow":
            assert not any(
                d.startswith(
                    ("keystone.platform.files.models", "keystone.platform.model_access.models")
                )
                for d in dependencies
            ), file


def test_all_platform_domains_are_checked():
    actual = {
        p.name for p in (ROOT / "platform").iterdir() if p.is_dir() and p.name != "__pycache__"
    }
    assert actual == set(DOMAINS)


def test_platform_and_business_modules_share_execution_boundaries():
    for family in ("platform", "businesses"):
        for module in (ROOT / family).iterdir():
            if module.is_dir() and module.name != "__pycache__":
                check_module_layers(module, f"keystone.{family}.{module.name}")


@pytest.mark.parametrize(
    "file,source",
    [
        ("services/records.py", "from keystone.core.execution.dispatch import enqueue_task"),
        ("services/records.py", "from keystone.core import execution"),
        ("services/records.py", "from ..tasks.workflows import register"),
        ("tasks/nested/sync.py", "from ...models import Record"),
        ("tasks/nested/sync.py", "from keystone.core.database import Database"),
        ("tasks/definitions.py", "from ..services.records import RecordService"),
        ("tasks/definitions.py", "from keystone.core.execution import dispatch"),
        ("controllers/records.py", "from ..models import Record"),
    ],
)
def test_business_module_checks_reject_execution_and_layer_bypasses(tmp_path, file, source):
    module = tmp_path / "probe"
    target = module / file
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf8")
    with pytest.raises(AssertionError):
        check_module_layers(module, "keystone.businesses.probe")


def test_module_checks_allow_lightweight_contracts_and_normal_service_symbols(tmp_path):
    module = tmp_path / "probe"
    file = module / "services" / "records.py"
    file.parent.mkdir(parents=True)
    file.write_text(
        "from keystone.core.execution.contracts import TaskDefinition\n"
        "from ..services.features import workflows_enabled\n",
        encoding="utf8",
    )
    check_module_layers(module, "keystone.businesses.probe")


def test_relative_imports_cannot_bypass_layers():
    tree = ast.parse("from .. import models\nfrom ..services.users import UserService")
    found = set(imports(tree, "keystone.platform.identity.controllers"))
    assert "keystone.platform.identity.models" in found
    assert "keystone.platform.identity.services.users.UserService" in found


def test_context_does_not_expose_identity_administration():
    from keystone.platform.flow.identity import ExecutionIdentity
    from keystone.platform.identity.access import AccessContext

    assert issubclass(ExecutionIdentity, AccessContext)
    for method in ("login", "create_user", "save_role", "issue_token", "reset_password"):
        assert not hasattr(AccessContext, method)
        assert not hasattr(ExecutionIdentity, method)


def test_model_access_protocols_and_services_are_independent_of_dbos():
    root = ROOT / "platform" / "model_access"
    for old in ("gateway.py", "providers.py", "billing.py", "transport.py", "jobs.py"):
        assert not (root / old).exists()
    for file in root.rglob("*.py"):
        package = ".".join(file.relative_to(ROOT.parent).with_suffix("").parts[:-1])
        dependencies = list(imports(ast.parse(file.read_text(encoding="utf8")), package))
        layer = file.relative_to(root).parts[0]
        if layer != "tasks":
            assert not any(d.startswith(("dbos", "keystone.worker")) for d in dependencies), file
        if layer in {"billing", "inference"}:
            assert not any(
                d.startswith("sqlalchemy")
                or d.startswith("keystone.platform.model_access.services")
                or d.startswith("keystone.platform.model_access.models")
                or d.startswith("keystone.platform.model_access.controllers")
                for d in dependencies
            ), file
            other = "inference" if layer == "billing" else "billing"
            assert not any(
                d.startswith(f"keystone.platform.model_access.{other}") for d in dependencies
            ), file
        if layer == "services":
            assert not any(d.startswith(("openai", "httpx2")) for d in dependencies), file


def test_system_task_adapters_do_not_own_domain_persistence():
    assert not list((ROOT / "worker" / "tasks").glob("*.py"))
    for name in ("files", "flow", "model_access"):
        folder = ROOT / "platform" / name / "tasks"
        for leaf in ("__init__.py", "definitions.py", "workflows.py"):
            file = folder / leaf
            assert file.is_file()
            dependencies = list(
                imports(
                    ast.parse(file.read_text(encoding="utf8")), f"keystone.platform.{name}.tasks"
                )
            )
            assert not any(d.startswith("sqlalchemy") or ".models" in d for d in dependencies)
            if leaf == "definitions.py":
                assert not any(d.startswith("dbos") or ".services" in d for d in dependencies)
    for file in (ROOT / "core" / "execution").glob("*.py"):
        dependencies = list(
            imports(ast.parse(file.read_text(encoding="utf8")), "keystone.core.execution")
        )
        assert not any(
            d.startswith(("keystone.platform", "keystone.composition")) for d in dependencies
        )


TESTS = Path(__file__).parents[1]
MODELS_ROOT = Path(__file__).parents[2] / "src" / "keystone"
CHECK = """
from pathlib import Path
from support.business_model_checks import check_business_models
check_business_models(Path(ROOT))
"""


def execute_check(root, setup=""):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(TESTS), str(MODELS_ROOT.parent))),
    }
    return subprocess.run(
        [sys.executable, "-c", f"ROOT = {str(root)!r}\n" + setup + CHECK],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_business_model_registration_and_layers():
    result = execute_check(MODELS_ROOT)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "case,message",
    [
        ("valid", None),
        ("missing_registration", "Model missing from load_models"),
        ("missing_export", "Model not exported"),
        ("wrong_schema", "metadata/schema"),
        ("wrong_prefix", "prefix mismatch"),
        ("service_import", "crosses layers"),
        ("controller_import", "crosses layers"),
    ],
)
def test_business_model_check_detects_mistakes(tmp_path, case, message):
    # An overlay exists only in pytest's temporary directory, never in production businesses/.
    root = tmp_path / "keystone"
    models = root / "businesses" / "probe" / "models"
    models.mkdir(parents=True)
    (models.parent / "__init__.py").write_text("", encoding="utf-8")
    (models / "__init__.py").write_text(
        "" if case == "missing_export" else "from .record import Record\n", encoding="utf-8"
    )
    table = "other_records" if case == "wrong_prefix" else "probe_records"
    schema = "public" if case == "wrong_schema" else "keystone"
    extra = {
        "service_import": "from ..services import save\n",
        "controller_import": "from ..controllers import router\n",
    }.get(case, "")
    (models / "record.py").write_text(
        extra + "from keystone.core.database import Base\n"
        "from sqlalchemy.orm import Mapped, mapped_column\n"
        "class Record(Base):\n"
        f"    __tablename__ = {table!r}\n"
        f"    __table_args__ = {{'schema': {schema!r}}}\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n",
        encoding="utf-8",
    )
    # Forbidden dependencies are not imported by the fake loader, letting the layer check report.
    register = case not in {"missing_registration", "service_import", "controller_import"}
    setup = f"""
import keystone.businesses
import keystone.model_registry
keystone.businesses.__path__.insert(0, {str(root / "businesses")!r})
original = keystone.model_registry.load_models
def load():
    metadata = original()
    if {register!r}:
        import keystone.businesses.probe.models.record
    return metadata
keystone.model_registry.load_models = load
"""
    result = execute_check(root, setup)
    if message is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0 and message in result.stderr, result.stderr
