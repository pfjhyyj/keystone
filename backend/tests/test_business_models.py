"""Fresh processes prevent prior API imports from hiding registration mistakes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "src" / "keystone"
CHECK = """
from pathlib import Path
from business_model_checks import check_business_models
check_business_models(Path(ROOT))
"""


def execute_check(root, setup=""):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(Path(__file__).parent), str(ROOT.parent))),
    }
    return subprocess.run(
        [sys.executable, "-c", f"ROOT = {str(root)!r}\n" + setup + CHECK],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_business_model_registration_and_layers():
    result = execute_check(ROOT)
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
