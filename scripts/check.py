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
ADMIN = ROOT / "frontend/admin"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", action="store_true", help="Explicitly omit DB and browser checks")
    parser.add_argument(
        "--deploy", action="store_true", help="Also build and verify isolated containers"
    )
    args = parser.parse_args()
    if args.unit and args.deploy:
        parser.error("--deploy requires full validation; omit --unit")
    if not args.unit:
        from sqlalchemy.engine import make_url

        try:
            url = make_url(os.environ.get("KEYSTONE_TEST_DATABASE_URL", ""))
            valid = url.drivername == "postgresql+psycopg" and url.database == "keystone_test"
        except Exception:
            valid = False
        if not valid:
            parser.error(
                "Full validation requires KEYSTONE_TEST_DATABASE_URL pointing to keystone_test "
                "using postgresql+psycopg. Use --unit only for an explicitly partial check."
            )
    node, uv = shutil.which("node"), shutil.which("uv")
    if not node or not uv:
        parser.error("Install Node.js and uv, then sync backend and frontend dependencies first")
    if args.deploy and not shutil.which("docker"):
        parser.error("--deploy requires Docker with Compose")
    directory = (
        ROOT
        / ".artifacts/checks"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    )
    temporary = directory / "tmp"
    temporary.mkdir(parents=True)
    environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
    results = {"mode": "unit" if args.unit else "full", "status": "failed", "checks": []}

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
                "scripts",
                "tests",
            ],
        )
        tests = [sys.executable, "-m", "pytest", "-q", "--junitxml", str(directory / "pytest.xml")]
        tests += ["-m", "not postgres"] if args.unit else ["--require-postgres"]
        run("backend tests", tests, ROOT / "backend")
        run(
            "frontend format",
            [node, "node_modules/prettier/bin/prettier.cjs", "--check", "."],
            ADMIN,
        )
        run("frontend tests", [node, "node_modules/vitest/vitest.mjs", "run"], ADMIN)
        run("frontend types", [node, "node_modules/vue-tsc/bin/vue-tsc.js", "--noEmit"], ADMIN)
        run("frontend build", [node, "node_modules/vite/bin/vite.js", "build"], ADMIN)
        run(
            "backend package",
            [uv, "build", "--out-dir", str(directory / "packages")],
            ROOT / "backend",
        )
        if not args.unit:
            run("admin browser", [sys.executable, str(ADMIN / "e2e/run.py")])
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
