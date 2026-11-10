"""Recursive architecture checks, including relative imports and internal adapters."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support.ast_support import imports
from support.module_checks import check_core_dependencies, check_module_layers

ROOT = Path(__file__).parents[2] / "src" / "keystone"
DOMAINS = ("audit", "browser", "dictionary", "files", "flow", "identity", "model_access")
LAYERS = ("controllers", "services", "models", "schemas")


@pytest.mark.parametrize("domain", DOMAINS)
def test_platform_module_contract_and_dependencies(domain):
    root = ROOT / "platform" / domain
    for layer in LAYERS:
        if not (root / layer).exists():
            continue
        assert (root / layer / "__init__.py").is_file()
        assert any(f.name != "__init__.py" for f in (root / layer).glob("*.py"))

    for file in root.rglob("*.py"):
        relative = file.relative_to(ROOT.parent).with_suffix("")
        package = ".".join(relative.parts[:-1])
        tree = ast.parse(file.read_text(encoding="utf-8"))
        dependencies = list(imports(tree, package))
        layer = file.relative_to(root).parts[0]
        if layer in {"controllers", "services", "schemas", "models"}:
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


def test_core_does_not_import_domains_or_application_assembly():
    check_core_dependencies(ROOT / "core")


@pytest.mark.parametrize(
    "source",
    [
        "from keystone.platform.files.services.files import FileService",
        "from ..platform.files import schemas",
        "from keystone import composition",
    ],
)
def test_core_rejects_upward_dependencies(tmp_path, source):
    (tmp_path / "probe.py").write_text(source, encoding="utf8")
    with pytest.raises(AssertionError, match="Core imports"):
        check_core_dependencies(tmp_path)


@pytest.mark.parametrize(
    "namespace,file,source,message",
    [
        (
            "keystone.platform.files",
            "services/probe.py",
            "from keystone.platform.identity.models import User",
            "another module",
        ),
        (
            "keystone.platform.files",
            "services/probe.py",
            "from ...identity import models",
            "another module",
        ),
        (
            "keystone.businesses.probe",
            "services/probe.py",
            "import keystone.platform.files.adapters.storage as storage",
            "another module",
        ),
        (
            "keystone.platform.files",
            "services/probe.py",
            "from keystone.businesses.probe.services.records import RecordService",
            "concrete business",
        ),
        (
            "keystone.businesses.probe",
            "adapters/dbos.py",
            "from dbos import DBOSClient",
            "execution implementation",
        ),
        (
            "keystone.platform.flow",
            "adapters/nested/dbos.py",
            "from dbos import DBOSClient",
            "execution implementation",
        ),
        (
            "keystone.platform.flow",
            "adapters/dbos.py",
            "from dbos import DBOSClient\nfrom dbos import DBOS",
            "execution implementation",
        ),
    ],
)
def test_module_ownership_and_client_exception_are_enforced(
    tmp_path, namespace, file, source, message
):
    target = tmp_path / file
    target.parent.mkdir(parents=True)
    for parent in target.parents:
        if parent == tmp_path:
            break
        (parent / "__init__.py").write_text("", encoding="utf8")
    target.write_text(source, encoding="utf8")
    with pytest.raises(AssertionError, match=message):
        check_module_layers(tmp_path, namespace)


@pytest.mark.parametrize(
    "file,source",
    [
        ("services/records.py", "from keystone.core.execution.dispatch import enqueue_task"),
        ("services/records.py", "from ..tasks.workflows import register"),
        ("tasks/nested/sync.py", "from ...models import Record"),
        ("tasks/nested/sync.py", "from keystone.core.database import Database"),
        ("tasks/definitions.py", "from ..services.records import RecordService"),
        ("tasks/definitions.py", "from keystone.core.execution import dispatch"),
        ("controllers/records.py", "from ..models import Record"),
        (
            "services/records.py",
            "from keystone.core import execution\n"
            "from keystone.core.execution.contracts import TaskDefinition",
        ),
        (
            "services/records.py",
            "import keystone.core.execution as execution\n"
            "from keystone.core.execution import contracts",
        ),
        ("tasks/definitions.py", "from openai import AsyncOpenAI"),
    ],
)
def test_business_module_checks_reject_execution_and_layer_bypasses(tmp_path, file, source):
    module = tmp_path / "probe"
    target = module / file
    target.parent.mkdir(parents=True)
    for parent in target.parents:
        if parent == tmp_path:
            break
        (parent / "__init__.py").write_text("", encoding="utf8")
    target.write_text(source, encoding="utf8")
    with pytest.raises(AssertionError):
        check_module_layers(module, "keystone.businesses.probe")


@pytest.mark.parametrize(
    "declaration",
    [
        "from keystone.core.execution.contracts import TaskDefinition",
        "from keystone.core.execution import contracts",
        "from ..tasks import definitions",
    ],
)
def test_module_checks_allow_lightweight_contracts_and_normal_service_symbols(
    tmp_path, declaration
):
    module = tmp_path / "probe"
    file = module / "services" / "records.py"
    file.parent.mkdir(parents=True)
    (file.parent / "__init__.py").write_text("", encoding="utf8")
    file.write_text(
        declaration + "\nfrom ..services.features import workflows_enabled\n",
        encoding="utf8",
    )
    check_module_layers(module, "keystone.businesses.probe")


@pytest.mark.parametrize(
    "file,source,message",
    [
        ("helpers.py", "def run(): pass", "Implementation must live"),
        ("catalog.py", "from .services.records import RecordService", "lightweight declarations"),
        (
            "catalog.py",
            "from keystone.core.catalog import Catalog\nCATALOG = Catalog()\nload()",
            "lightweight declarations",
        ),
        ("__init__.py", "def hidden_service(): pass", "lightweight declarations"),
        ("schemas/records.py", "from sqlalchemy import select", "runtime infrastructure"),
        ("adapters/sdk.py", "from ..services.records import save", "business Service"),
        ("adapters/sdk.py", "from ..models import Record", "domain persistence"),
        (
            "services/records.py",
            "from ..controllers.dependencies import access_context",
            "HTTP/controller",
        ),
        ("workflows/internal/workflow.py", "from ...models import Record", "domain persistence"),
        (
            "controllers/records.py",
            "from keystone.core.database import Database",
            "imports persistence",
        ),
        ("models/record.py", "from ..adapters.storage import Client", "runtime adapter"),
        (
            "services/records.py",
            "from keystone.composition import execution_version",
            "application assembly",
        ),
    ],
)
def test_mvc_structure_rejects_root_implementations_and_reverse_dependencies(
    tmp_path,
    file,
    source,
    message,
):
    root = tmp_path / "probe"
    target = root / file
    target.parent.mkdir(parents=True)
    for parent in target.parents:
        if parent == tmp_path:
            break
        (parent / "__init__.py").write_text("", encoding="utf8")
    target.write_text(source, encoding="utf8")
    with pytest.raises(AssertionError, match=message):
        check_module_layers(root, "keystone.businesses.probe")


def test_module_metadata_is_lightweight_and_engine_adapter_uses_only_client(tmp_path):
    root = tmp_path / "probe"
    root.mkdir()
    (root / "__init__.py").write_text('"""Module package."""', encoding="utf8")
    (root / "catalog.py").write_text(
        "from keystone.core.catalog import Catalog\n"
        "CATALOG = Catalog(audit_actions={'probe.record.save': set()})",
        encoding="utf8",
    )
    adapters = root / "adapters"
    adapters.mkdir()
    (adapters / "__init__.py").write_text("", encoding="utf8")
    (adapters / "dbos.py").write_text("from dbos import DBOSClient", encoding="utf8")
    check_module_layers(root, "keystone.platform.flow")


def test_flow_definitions_and_module_metadata_do_not_load_runtime_implementations():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys\n"
            f"for name in {DOMAINS!r}:\n"
            "    importlib.import_module(f'keystone.platform.{name}.catalog')\n"
            "import keystone.platform.flow.schemas.definitions\n"
            "for name in sys.modules:\n"
            "    assert not name.startswith(('dbos', 'sqlalchemy', 'pydantic_ai')), name\n"
            "    assert '.services' not in name and '.models' not in name, name\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_flow_services_import_without_application_assembly_or_executor():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys\n"
            "for name in ('runs', 'identity', 'reconciliation', 'artifacts', 'resources'):\n"
            "    importlib.import_module(f'keystone.platform.flow.services.{name}')\n"
            "for name in sys.modules:\n"
            "    assert not name.startswith(('keystone.composition', 'keystone.bootstrap', "
            "'keystone.entrypoints', 'keystone.worker', 'dbos')), name\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_relative_imports_cannot_bypass_layers():
    tree = ast.parse("from .. import models\nfrom ..services.users import UserService")
    found = set(imports(tree, "keystone.platform.identity.controllers"))
    assert "keystone.platform.identity.models" in found
    assert "keystone.platform.identity.services.users.UserService" in found


def test_model_access_protocols_and_services_are_independent_of_dbos():
    root = Path(__file__).parents[2] / "src/keystone/platform/model_access"
    assert (root / "channels/__init__.py").is_file()
    assert (root / "channels/spec.py").is_file()
    files = [root / "schemas/errors.py", *(root / "channels").rglob("*.py")]
    files += [p for p in (root / "adapters/billing").rglob("*.py") if p.name != "registry.py"]
    for path in files:
        package = "keystone.platform.model_access." + ".".join(path.relative_to(root).parts[:-1])
        dependencies = list(imports(ast.parse(path.read_text(encoding="utf8")), package))
        assert not any(
            name.startswith(
                (
                    "sqlalchemy",
                    "dbos",
                    "fastapi",
                    "keystone.core.errors",
                    "keystone.core.database",
                    "keystone.core.execution",
                    "keystone.composition",
                    "keystone.bootstrap",
                )
            )
            or ".services" in name
            or ".models" in name
            or ".workflows" in name
            or name.startswith("keystone.platform.identity")
            or name.startswith("keystone.platform.model_access.adapters")
            for name in dependencies
        ), path

    root = ROOT / "platform" / "model_access"
    for file in root.rglob("*.py"):
        package = ".".join(file.relative_to(ROOT.parent).with_suffix("").parts[:-1])
        dependencies = list(imports(ast.parse(file.read_text(encoding="utf8")), package))
        layer = file.relative_to(root).parts[0]
        if layer != "tasks":
            assert not any(d.startswith(("dbos", "keystone.worker")) for d in dependencies), file
        if (
            layer == "adapters"
            and len(file.relative_to(root).parts) > 2
            and file.relative_to(root).parts[1] in {"billing", "inference"}
        ):
            assert not any(
                d.startswith("sqlalchemy")
                or d.startswith("keystone.platform.model_access.services")
                or d.startswith("keystone.platform.model_access.models")
                or d.startswith("keystone.platform.model_access.controllers")
                for d in dependencies
            ), file
            other = "inference" if file.relative_to(root).parts[1] == "billing" else "billing"
            assert not any(
                d.startswith(f"keystone.platform.model_access.adapters.{other}")
                for d in dependencies
            ), file
        if layer == "services":
            assert not any(d.startswith(("openai", "httpx2")) for d in dependencies), file


def test_model_access_provider_validation_is_lazy_and_does_not_load_adapter_hosts():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from pydantic import TypeAdapter, ValidationError\n"
            # Existing structured DTOs load ServiceError; baseline their dependencies
            # before checking what lazy registry validation adds.
            "from keystone.platform.model_access.schemas import structured\n"
            "from keystone.platform.model_access.schemas.types import Provider\n"
            "prefix = 'keystone.platform.model_access'\n"
            "assert not any(n.startswith(prefix + '.channels') for n in sys.modules)\n"
            "baseline = set(sys.modules)\n"
            "validator = TypeAdapter(Provider)\n"
            "for provider in ('deepseek', 'openrouter', 'aliyun_bailian', 'newapi', "
            "'openlux', 'yunwu', 'volcengine'):\n"
            "    assert validator.validate_python(provider) == provider\n"
            "try:\n"
            "    validator.validate_python('__uninstalled_channel__')\n"
            "except ValidationError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('Provider accepted an uninstalled channel')\n"
            "for name in set(sys.modules) - baseline:\n"
            "    assert not name.startswith((prefix + '.adapters', 'dbos', 'sqlalchemy', "
            "'keystone.core.errors', 'keystone.core.execution')), name\n"
            "    assert '.services' not in name and '.models' not in name, name\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


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
