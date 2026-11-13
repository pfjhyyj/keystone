"""Repository checks. Full validation requires an explicit dedicated PostgreSQL test database."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "frontend/console"


def probe_test_database(workers):
    sys.path.insert(0, str(ROOT / "backend/tests"))
    from support.postgres import probe_test_database as probe

    return probe(workers=workers)


def test_workers():
    value = os.environ.get("KEYSTONE_TEST_WORKERS")
    if value:
        try:
            workers = int(value)
        except ValueError as error:
            raise SystemExit("KEYSTONE_TEST_WORKERS must be an integer") from error
        if workers < 1:
            raise SystemExit("KEYSTONE_TEST_WORKERS must be at least 1")
        return min(workers, 8)
    return max(1, min(8, os.cpu_count() or 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=("full", "backend", "frontend", "browser"),
        default="full",
        help="Run an explicit component; full remains the default",
    )
    parser.add_argument("--unit", action="store_true", help="Explicitly omit DB and browser checks")
    parser.add_argument(
        "--deploy", action="store_true", help="Also build and verify isolated containers"
    )
    args = parser.parse_args()
    if args.unit and args.deploy:
        parser.error("--deploy requires full validation; omit --unit")
    if args.deploy and args.scope != "full":
        parser.error("--deploy requires --scope full")
    if args.unit and args.scope not in {"full", "backend"}:
        parser.error("--unit only applies to full or backend checks")
    backend = args.scope in {"full", "backend"}
    frontend = args.scope in {"full", "frontend"}
    browser = args.scope in {"full", "browser"} and not args.unit
    workers = test_workers() if backend else 1
    probe = None
    if (backend and not args.unit) or browser:
        probe = probe_test_database(workers)
        if not probe.available:
            parser.error(
                f"{probe.requirement()} Start the dedicated keystone_test database "
                "or use --unit for an explicitly partial check."
            )
    node, uv = shutil.which("node"), shutil.which("uv")
    if (frontend or browser) and not node:
        parser.error("Install Node.js and sync frontend dependencies first")
    if backend and not uv:
        parser.error("Install uv and sync backend dependencies first")
    if args.deploy:
        if not shutil.which("docker"):
            parser.error("--deploy requires Docker with Compose")
        try:
            subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            parser.error("--deploy requires a running Docker daemon")
    directory = (
        ROOT
        / ".artifacts/checks"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    )
    temporary = directory / "tmp"
    temporary.mkdir(parents=True)
    uv_cache = ROOT / ".cache/uv"
    uv_cache.mkdir(parents=True, exist_ok=True)
    environment = dict(
        os.environ,
        TEMP=str(temporary),
        TMP=str(temporary),
        TMPDIR=str(temporary),
        UV_CACHE_DIR=str(uv_cache),
    )
    results = {
        "mode": ("unit" if args.unit else "full") if args.scope == "full" else args.scope,
        "scope": args.scope,
        "unit_only": args.unit,
        "status": "failed",
        "test_workers": workers,
        "postgres_connection_capacity": probe.connection_capacity if probe else None,
        "checks": [],
    }

    def run(label, command, cwd=ROOT):
        print(f"\n[{label}]", flush=True)
        started = monotonic()
        result = subprocess.run(command, cwd=cwd, env=environment, check=False)
        results["checks"].append(
            {
                "name": label,
                "exit_code": result.returncode,
                "seconds": round(monotonic() - started, 2),
            }
        )
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command)

    print(f"Check artifacts: {directory}", flush=True)
    try:
        if frontend:
            run(
                "frontend format",
                [node, "node_modules/prettier/bin/prettier.cjs", "--check", "."],
                CONSOLE,
            )
            run("frontend tests", [node, "node_modules/vitest/vitest.mjs", "run"], CONSOLE)
            run(
                "frontend types", [node, "node_modules/vue-tsc/bin/vue-tsc.js", "--noEmit"], CONSOLE
            )
            run("frontend build", [node, "node_modules/vite/bin/vite.js", "build"], CONSOLE)
        if backend:
            run(
                "backend lint",
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--config",
                    "backend/pyproject.toml",
                    "backend",
                    "examples",
                    "scripts",
                    "tests",
                ],
            )
            run(
                "backend format",
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "format",
                    "--config",
                    "backend/pyproject.toml",
                    "--check",
                    "backend",
                    "examples",
                    "scripts",
                    "tests",
                ],
            )
            tests = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--junitxml",
                str(directory / "pytest.xml"),
                "--durations=20",
            ]
            tests += ["-m", "not postgres"] if args.unit else ["--require-postgres"]
            if workers > 1:
                tests += ["-n", str(workers), "--dist", "load"]
                if not args.unit:
                    # Keep long Worker/recovery cases available to free executors instead
                    # of reserving a large consecutive batch on one pytest worker.
                    tests += ["--maxschedchunk=1"]
            run("backend tests", tests, ROOT / "backend")
            run(
                "backend package",
                [uv, "build", "--out-dir", str(directory / "packages")],
                ROOT / "backend",
            )
        if browser:
            run("console browser", [sys.executable, str(CONSOLE / "e2e/run.py")])
        if args.deploy:
            run("deployment and recovery", [sys.executable, "tests/deploy/verify.py"])
        results["status"] = "passed"
    except (subprocess.CalledProcessError, OSError):
        print("Validation failed; see the check output and result.json.", file=sys.stderr)
        return 1
    finally:
        (directory / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"PASS {results['mode']} validation ({len(results['checks'])} checks)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
