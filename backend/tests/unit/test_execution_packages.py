"""Package manifests and actual loaded resources share one execution fingerprint."""

import importlib
import sys
from dataclasses import replace

import pytest

from keystone import composition
from keystone.bootstrap import build_runtime
from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.packages import FlowPackage, PackageResource


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "external_workflows"
    root.mkdir()
    (root / "__init__.py").write_text("raise AssertionError('must not import execution code')\n")
    (root / "child.py").write_bytes(b"# native supervisor\n")
    (root / "rules.bin").write_bytes(b"\x00\x01\r\n\xff")
    monkeypatch.syspath_prepend(str(tmp_path))
    manifest = FlowPackage(
        key="external",
        host_api_version=1,
        definitions=(),
        execution_modules=("external_workflows.child",),
        execution_packages=("external_workflows",),
        required_resources=(PackageResource("external_workflows", "child.py"),),
    )
    monkeypatch.setattr(composition, "FLOW_PACKAGES", (manifest,))
    importlib.invalidate_caches()
    composition.execution_version.cache_clear()
    yield root
    composition.execution_version.cache_clear()


def version():
    composition.execution_version.cache_clear()
    return composition.execution_version()


def test_code_binary_resources_and_supervisor_are_hashed_without_importing(package):
    before = version()
    assert "external_workflows" not in sys.modules
    (package / "__pycache__").mkdir()
    (package / "__pycache__/ignored.pyc").write_bytes(b"ignored")
    assert version() == before
    (package / "child.py").write_bytes(b"# native supervisor\r\n")
    assert version() == before
    (package / "child.py").write_bytes(b"# changed supervisor\n")
    code = version()
    assert code != before
    (package / "rules.bin").write_bytes(b"\x00\x02\r\n\xff")
    assert version() != code


@pytest.mark.parametrize("missing", ["child.py", "__init__.py"])
def test_missing_package_or_required_resource_prevents_startup(package, missing):
    (package / missing).unlink()
    with pytest.raises(ServiceError):
        build_runtime(Settings(config_file=None, database_url=None))


def test_resource_boundaries_and_host_version_fail_closed(package, monkeypatch):
    for path in ("../outside", "/absolute", "a/../../escape"):
        with pytest.raises(ValueError):
            PackageResource("external_workflows", path)
    with pytest.raises(ValueError):
        replace(composition.FLOW_PACKAGES[0], host_api_version=2)
    (package / "child.py").unlink()
    (package / "child.py").symlink_to(package / "rules.bin")
    with pytest.raises(ServiceError):
        version()


def test_text_normalization_across_stream_boundary(package):
    (package / "child.py").write_bytes(b"a" * (1024 * 1024 - 1) + b"\r\nnext\r")
    before = version()
    (package / "child.py").write_bytes(b"a" * (1024 * 1024 - 1) + b"\nnext\r")
    assert version() == before


def test_installed_backend_is_hashed_instead_of_shadow_sources(package, tmp_path, monkeypatch):
    application = tmp_path / "application"
    installed = application / ".venv/lib/site-packages/keystone/composition.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("# installed backend\n")
    shadow = application / "src/keystone/catalog.py"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("# stale source tree\n")
    (application / "uv.lock").write_text("# locked dependencies\n")
    monkeypatch.setattr(composition, "__file__", str(installed))
    monkeypatch.setattr(composition, "FLOW_PACKAGES", ())
    before = version()
    installed.write_text("# different installed backend\n")
    actual = version()
    assert actual != before
    shadow.write_text("# changed unused source copy\n")
    assert version() == actual
