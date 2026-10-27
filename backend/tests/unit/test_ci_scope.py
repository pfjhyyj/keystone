"""CI selection must omit unrelated work without hiding required validation."""

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["frontend/console/src/views/models/ModelsView.vue"], {"frontend", "browser"}),
        (["frontend/console/pnpm-lock.yaml"], {"frontend", "browser"}),
        (["backend/src/keystone/platform/files/services/files.py"], {"backend", "browser"}),
        (["backend/uv.lock"], {"backend", "browser"}),
        (["backend/src/keystone/examples/document_review/README.md"], {"backend", "browser"}),
        (["README.md", "docs/development.md", ".impeccable/mocks/overview.png"], set()),
        ([".github/workflows/check.yml"], {"backend", "frontend", "browser"}),
        (["scripts/check.py"], {"backend", "frontend", "browser"}),
        (["unknown-root-config.toml"], {"backend", "frontend", "browser"}),
        (
            ["backend/pyproject.toml", "frontend/console/package.json"],
            {"backend", "frontend", "browser"},
        ),
        ([], set()),
    ],
)
def test_changed_paths_preserve_the_affected_components(paths, expected):
    assert load_script("ci_scope").classify_changes(paths) == expected


def test_failed_comparison_runs_every_check(monkeypatch, tmp_path, capsys):
    scope = load_script("ci_scope")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": "0" * 40, "after": "1" * 40}), encoding="utf-8")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["ci_scope.py"])
    assert scope.main() == 0
    assert "comparison unavailable" in json.loads(capsys.readouterr().out)["reason"]
    assert output.read_text(encoding="utf-8").splitlines() == [
        "backend=true",
        "frontend=true",
        "browser=true",
    ]


def test_manual_validation_never_uses_a_path_skip(monkeypatch, tmp_path):
    scope = load_script("ci_scope")
    event = tmp_path / "event.json"
    event.write_text("{}", encoding="utf-8")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["ci_scope.py"])
    assert scope.main() == 0
    assert output.read_text(encoding="utf-8").count("=true") == 3


def test_renaming_source_into_documentation_keeps_source_checks(tmp_path):
    scope = load_script("ci_scope")

    def git(*args):
        return (
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
            .stdout.decode()
            .strip()
        )

    git("init")
    source = tmp_path / "frontend/console/src/App.vue"
    source.parent.mkdir(parents=True)
    source.write_text("<template>Console</template>", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "source")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    source.rename(tmp_path / "docs/App.vue")
    git("add", "-A")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "move")
    files = scope.changed_files(base, git("rev-parse", "HEAD"), cwd=tmp_path)
    assert scope.classify_changes(files) == {"frontend", "browser"}


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


@pytest.mark.parametrize(
    ("selected", "results", "deployment", "passed"),
    [
        (set(), {}, False, True),
        ({"frontend", "browser"}, {"frontend": "success", "browser": "success"}, False, True),
        ({"backend"}, {"backend": "skipped"}, False, False),
        ({"frontend"}, {"frontend": "failure"}, False, False),
        ({"browser"}, {"browser": "cancelled"}, False, False),
        (set(), {"changes": "failure"}, False, False),
        (set(), {"deployment": "skipped"}, True, False),
        (set(), {"deployment": "success"}, True, True),
    ],
)
def test_required_check_never_accepts_missing_or_failed_selected_jobs(
    selected, results, deployment, passed
):
    workflow = (ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8")
    code = textwrap.dedent(
        workflow.split("          python - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0]
    )
    jobs = {
        name: {"result": results.get(name, "skipped")}
        for name in ("backend", "frontend", "browser", "deployment")
    }
    jobs["changes"] = {
        "result": results.get("changes", "success"),
        "outputs": {
            name: str(name in selected).lower() for name in ("backend", "frontend", "browser")
        },
    }
    result = subprocess.run(
        [sys.executable, "-O", "-c", code],
        env={**os.environ, "RESULTS": json.dumps(jobs), "DEPLOYMENT": str(deployment).lower()},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is passed, result.stderr.decode()
