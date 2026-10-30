import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("scope", ["frontend", "backend", "browser", "full"])
def test_explicit_component_only_requires_its_own_tools(monkeypatch, tmp_path, scope):
    check = load_script("check")
    commands = []
    probes = []
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "CONSOLE", tmp_path / "console")
    monkeypatch.setattr(check, "test_workers", lambda: 2)

    def probe(workers):
        probes.append(workers)
        return SimpleNamespace(available=True, connection_capacity=512)

    monkeypatch.setattr(check, "probe_test_database", probe)
    tools = {"frontend": {"node"}, "backend": {"uv"}, "browser": {"node"}, "full": {"node", "uv"}}
    monkeypatch.setattr(check.shutil, "which", lambda name: name if name in tools[scope] else None)
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["check.py", "--scope", scope])
    assert check.main() == 0
    report = json.loads(next(tmp_path.glob(".artifacts/checks/*/result.json")).read_text())
    assert report["mode"] == scope
    assert report["status"] == "passed"
    assert bool(probes) == (scope != "frontend")
    assert any("pytest" in command for command in commands) == (scope in {"backend", "full"})
    assert any("--require-postgres" in command for command in commands) == (
        scope in {"backend", "full"}
    )
    assert any(
        any(Path(arg).name == "run.py" and Path(arg).parent.name == "e2e" for arg in command)
        for command in commands
    ) == (scope in {"browser", "full"})
    assert any("build" in command and "uv" in command for command in commands) == (
        scope in {"backend", "full"}
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--scope", "frontend", "--unit"],
        ["--scope", "browser", "--unit"],
        ["--scope", "backend", "--deploy"],
    ],
)
def test_partial_options_cannot_silently_change_the_requested_validation(monkeypatch, args):
    check = load_script("check")
    monkeypatch.setattr(sys, "argv", ["check.py", *args])
    with pytest.raises(SystemExit, match="2"):
        check.main()
