"""Real images + disposable Compose projects; test-only HTTP transport injection.

Run from the repository root with backend/.venv/bin/python tests/deploy/verify.py.
The complete stack is a test fixture, separate from development dependency Compose.
Like Ether's deployment check, use PostgreSQL and tar directly, not a backup framework.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "tests/deploy/compose.yaml"
BACKEND_SERVICES = ("api", "worker", "init")
PASSWORD = "deploy-test-password-only"
SOURCE = b"A document for isolated deployment recovery verification.\n"


def wait(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    raise AssertionError("Deployment condition timed out")


def command(args, log, *, env=None, input=None, check=True, timeout=180):
    result = subprocess.run(
        args,
        check=False,
        cwd=ROOT,
        env=env,
        input=input.encode("utf-8") if input is not None else None,
        capture_output=True,
        timeout=timeout,
    )
    # Binary stdin keeps Windows from inserting CR into Linux getpass passwords.
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    with log.open("a", encoding="utf-8") as output:
        output.write(result.stdout + result.stderr)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); see {log}")
    return result


def request(client, method, path, status=200, **kwargs):
    response = client.request(method, "/api/v1" + path, **kwargs)
    assert response.status_code == status, (
        method,
        path,
        response.status_code,
        response.text,
    )
    return response.json() if response.content else None


def login(client, username="admin", password=PASSWORD):
    data = request(client, "POST", "/auth/login", json={"username": username, "password": password})
    client.headers["X-CSRF-Token"] = data["csrf_token"]
    return data


class Deployment:
    def __init__(self, directory, images, password):
        self.project = "keystone-verify-" + uuid4().hex[:12]
        self.directory = directory / self.project
        self.directory.mkdir()
        self.log = self.directory / "compose.log"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.images = images
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("KEYSTONE_", "COMPOSE_", "POSTGRES_"))
        }
        self.environment.update(
            {
                "POSTGRES_PASSWORD": password,
                "KEYSTONE_DATABASE_URL": f"postgresql+psycopg://keystone:{password}@postgres:5432/keystone",
                "KEYSTONE_ENV": "test",
                "KEYSTONE_INITIAL_ADMIN_PASSWORD": PASSWORD,
                "KEYSTONE_FLOW_ENABLED": "true",
                "KEYSTONE_BROWSER_ORIGINS": json.dumps([self.origin]),
            }
        )
        # No deploy/.env, development ports, existing volumes or mutable image tags.
        (self.directory / "empty.env").write_text("", encoding="utf-8")
        self.override = self.directory / "override.yaml"
        lines = ["services:"]
        for service in BACKEND_SERVICES:
            lines += [f"  {service}:", f"    image: {images['backend']}"]
            if service in {"api", "worker"}:
                entrypoint = "api" if service == "api" else "worker"
                script = (
                    "import sys; sys.path.insert(0, '/test-support'); "
                    "from support.model_stub import install; install(); "
                    "from support.flow_fixture import install; install(); "
                    f"from keystone.entrypoints.{entrypoint} import main; main()"
                )
                lines += [
                    "    command: " + json.dumps(["python", "-c", script]),
                    "    volumes:",
                    "      - type: bind",
                    "        source: " + json.dumps(str(ROOT / "backend/tests/support")),
                    "        target: /test-support/support",
                    "        read_only: true",
                ]
            if service == "api":
                lines += ["    ports: !override []"]
        lines += [
            "  postgres:",
            f"    image: {images['postgres']}",
            "    ports: !override []",
            "  console:",
            f"    image: {images['console']}",
            f'    ports: !override ["127.0.0.1:{port}:80"]',
        ]
        self.override.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def compose(self, *args, extra=None, **kwargs):
        cmd = [
            "docker",
            "compose",
            "--env-file",
            str(self.directory / "empty.env"),
            "-p",
            self.project,
            "-f",
            str(COMPOSE),
            "-f",
            str(self.override),
        ]
        if extra:
            cmd += ["-f", str(extra)]
        return command([*cmd, *args], self.log, env=self.environment, **kwargs)

    def up(self):
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "90",
        )
        wait(lambda: "flow_worker_ready" in self.compose("logs", "worker").stdout)
        self.compose("exec", "-T", "worker", "keystone-worker", "--check")

    def client(self):
        return httpx.Client(
            base_url=self.origin,
            headers={"Origin": self.origin, "X-Keystone-Request": "1"},
            timeout=15,
            trust_env=False,
        )

    def container(self, service):
        value = self.compose("ps", "-a", "-q", service).stdout.strip()
        assert re.fullmatch(r"[0-9a-f]{64}", value)
        return value

    def sql(self, query):
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "keystone",
            "-d",
            "keystone",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            query,
        ).stdout.strip()

    def finish(self, client, identifier):
        def terminal():
            row = request(client, "GET", "/flow-runs/" + identifier)
            return row if row["status"] in {"succeeded", "failed", "cancelled"} else None

        row = wait(terminal)
        assert row["status"] == "succeeded", row
        return row

    def archive_files(self, backup, *, restore=False):
        # Only this freshly allocated, UUID-named project's volume is ever written.
        assert re.fullmatch(r"keystone-verify-[0-9a-f]{12}", self.project)
        volume = self.project + "_file_data"
        label = command(
            [
                "docker",
                "volume",
                "inspect",
                "-f",
                '{{index .Labels "com.docker.compose.project"}}',
                volume,
            ],
            self.log,
        ).stdout.strip()
        assert label == self.project
        running = self.compose("ps", "--status", "running", "--services").stdout.split()
        assert running == ["postgres"], running
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={volume},dst=/data/files" + ("" if restore else ",readonly"),
                "--mount",
                f"type=bind,src={backup.as_posix()},dst=/backup" + (",readonly" if restore else ""),
                "--entrypoint",
                "tar",
                self.images["backend"],
                "-C",
                "/data/files",
                *(["-xpf", "/backup/files.tar"] if restore else ["-cpf", "/backup/files.tar", "."]),
            ],
            self.log,
        )

    def cleanup(self):
        self.compose("logs", "--no-color", check=False)
        # The name is generated here; never accept an existing project name from the caller.
        assert re.fullmatch(r"keystone-verify-[0-9a-f]{12}", self.project)
        self.compose("down", "--volumes", "--remove-orphans")


def verify_api_recreation(deployment):
    """Keep console running while the API moves to a different Docker address."""
    console = deployment.container("console")
    api = deployment.container("api")
    address = command(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            api,
        ],
        deployment.log,
    ).stdout.strip()
    holder = deployment.project + "-api-address-holder"
    with deployment.client() as client:
        request(client, "GET", "/ready")
        deployment.compose("stop", "api")
        deployment.compose("rm", "-f", "api")
        try:
            # Occupy the old address so this cannot accidentally pass through IP reuse.
            command(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    holder,
                    "--network",
                    deployment.project + "_default",
                    "--ip",
                    address,
                    "--entrypoint",
                    "sleep",
                    deployment.images["backend"],
                    "180",
                ],
                deployment.log,
            )
            deployment.compose(
                "up", "-d", "--no-build", "--pull", "never", "--no-deps", "--wait", "api"
            )
            assert deployment.container("console") == console
            assert deployment.container("api") != api
            request(client, "GET", "/ready")
            login(client)
        finally:
            command(["docker", "rm", "-f", holder], deployment.log, check=False)


def exercise(source, restored, checks):
    def passed(message):
        checks.append(message)
        print("PASS " + message, flush=True)

    dependencies = json.loads(
        command(
            [
                "docker",
                "compose",
                "--env-file",
                str(source.directory / "empty.env"),
                "-f",
                str(ROOT / "deploy/compose.yaml"),
                "config",
                "--format",
                "json",
            ],
            source.log,
            env={
                key: value
                for key, value in source.environment.items()
                if not key.startswith(("KEYSTONE_", "POSTGRES_", "COMPOSE_"))
            },
        ).stdout
    )
    assert set(dependencies["services"]) == {"postgres"}
    assert "build" not in dependencies["services"]["postgres"]
    passed("development Compose contains only PostgreSQL and needs no application configuration")

    source.compose("up", "-d", "--wait", "postgres")
    failure = source.directory / "failure.json"
    failure.write_text(
        json.dumps({"services": {"init": {"command": ["python", "-c", "raise SystemExit(23)"]}}}),
        encoding="utf-8",
    )
    result = source.compose(
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "api",
        "worker",
        "console",
        extra=failure,
        check=False,
    )
    assert result.returncode != 0
    assert not (
        {"api", "worker", "console"}
        & set(source.compose("ps", "--status", "running", "--services").stdout.split())
    )
    passed("failed migration blocks API, Worker and console")
    source.up()
    source.compose("run", "--rm", "--no-deps", "init")
    for service in ("api", "worker"):
        assert source.compose("exec", "-T", service, "id", "-u").stdout.strip() == "10001"
    assert (
        source.sql(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='keystone' "
            "AND table_name <> 'alembic_version'"
        )
        == "17"
    )
    passed("empty-volume cold start, repeatable migrations/storage init, non-root processes")
    verify_api_recreation(source)
    passed("Nginx follows a recreated API at a different address without restarting console")

    with source.client() as client:
        for path, status in (("/", 302), ("/console", 301)):
            response = client.get(path)
            assert response.status_code == status
            assert response.headers["location"] == "/console/"
        assert client.get("/console/flows").status_code == 200
        html = client.get("/console/").text
        assets = re.findall(r'(?:src|href)="(/console/assets/[^\"]+)"', html)
        assert assets and all(client.get(asset).status_code == 200 for asset in assets)
        request(client, "GET", "/ready")
        login(client)
        assert "role:read" in request(client, "GET", "/auth/me")["permissions"]
        # CSRF still applies when requests go through the actual Nginx entrypoint.
        assert (
            client.post(
                "/api/v1/dictionaries",
                headers={"X-CSRF-Token": "invalid"},
                json={"key": "bad", "name": "Bad"},
            ).status_code
            == 403
        )
        file = request(
            client,
            "POST",
            "/files",
            201,
            params={"name": "source.txt", "expected_size": len(SOURCE)},
            content=SOURCE,
        )
        role = request(
            client,
            "POST",
            "/roles",
            201,
            json={
                "key": "reader",
                "name": "Reader",
                "permissions": ["file:inspect", "file:content_read", "dictionary:read"],
            },
        )
        user = request(
            client,
            "POST",
            "/users",
            201,
            json={"username": "reader", "display_name": "Reader", "password": PASSWORD},
        )
        request(
            client,
            "PUT",
            f"/users/{user['id']}/roles/{role['id']}",
            204,
            json={"assigned": True},
        )
        for action in ("inspect", "content_read"):
            request(
                client,
                "POST",
                f"/files/{file['id']}/grants",
                201,
                json={"subject_type": "role", "role_id": role["id"], "action": action},
            )
        dictionary = request(
            client,
            "POST",
            "/dictionaries",
            201,
            json={"key": "review", "name": "Review"},
        )
        item = request(
            client,
            "POST",
            f"/dictionaries/{dictionary['id']}/items",
            201,
            json={"value": "done", "label": "Done"},
        )
        for provider in ("openrouter", "deepseek", "aliyun_bailian"):
            channel = request(
                client,
                "POST",
                "/model-channels",
                201,
                json={
                    "provider": provider,
                    "name": provider,
                    "api_base": "https://openrouter.ai/api/v1",
                    "api_key": "fake-inference",
                    "billing_api_key": "fake-billing",
                },
            )
            request(
                client,
                "POST",
                "/model-entries",
                201,
                json={
                    "key": provider,
                    "channel_id": channel["id"],
                    "provider_model": "mock-chat",
                },
            )
            request(client, "POST", f"/model-channels/{channel['id']}/balance")
            response = client.post(
                "/api/v1/models/invoke",
                json={
                    "model_key": provider,
                    "stream": provider == "openrouter",
                    "messages": [{"role": "user", "content": "Check"}],
                },
            )
            assert response.status_code == 200 and "模拟渠道响应" in response.text
            if provider == "openrouter":
                assert response.headers["content-type"].startswith("text/event-stream")
        passed(
            "Nginx assets/deep links, login/CSRF, files, roles, dictionary, "
            "three mock providers/SSE"
        )
        command(
            ["node", str(ROOT / "tests/deploy/browser.mjs"), source.origin, str(source.directory)],
            source.log,
        )
        passed("Chromium login, deep-link refresh and Flow page through built Nginx assets")
        payload = {
            "definition_key": "document.review",
            "request_key": "completed-before-backup",
            "input": {"source_file_id": file["id"], "model_key": "openrouter"},
        }
        run = request(client, "POST", "/flow-runs", 202, json=payload)
        completed = source.finish(client, run["id"])
        report = completed["result"]["report_file_id"]
        report_bytes = client.get(f"/api/v1/files/{report}/content").content
        assert report_bytes
        steps = request(client, "GET", f"/flow-runs/{run['id']}/steps")
        assert steps["available"] and steps["items"]
        costs = request(client, "GET", f"/flow-runs/{run['id']}/costs")
        invocations = request(client, "GET", "/model-invocations")
        assert len(invocations) == 5
        channels = request(client, "GET", "/model-channels")
        refs = request(client, "GET", f"/files/{file['id']}/references")
        assert refs
        # Change the test reader's initial password before backing up its session/roles.
        with source.client() as reader:
            login(reader, "reader")
            request(
                reader,
                "POST",
                "/auth/password",
                204,
                json={"current_password": PASSWORD, "password": PASSWORD + "-changed"},
            )
            login(reader, "reader", PASSWORD + "-changed")
            assert reader.get(f"/api/v1/files/{file['id']}/content").content == SOURCE
            assert reader.get("/api/v1/users").status_code == 403
        source.compose("stop", "worker")
        pending_payload = {**payload, "request_key": "pending-before-backup"}
        pending = request(client, "POST", "/flow-runs", 202, json=pending_payload)
        assert pending["status"] == "pending"
        passed("real DBOS/Harness completion and durable pending Flow")

        source.compose("stop", "console", "api", "worker")
        backup = source.directory / "backup"
        backup.mkdir()
        source.compose(
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            "keystone",
            "-d",
            "keystone",
            "-Fc",
            "-f",
            "/tmp/database.dump",
        )
        command(
            [
                "docker",
                "cp",
                source.container("postgres") + ":/tmp/database.dump",
                str(backup / "database.dump"),
            ],
            source.log,
        )
        source.archive_files(backup)
        checksums = {
            name: hashlib.sha256((backup / name).read_bytes()).hexdigest()
            for name in ("database.dump", "files.tar")
        }
        (backup / "manifest.json").write_text(
            json.dumps({"images": source.images, "sha256": checksums}, indent=2),
            encoding="utf-8",
        )

        restored.compose("up", "-d", "--wait", "postgres")
        command(
            [
                "docker",
                "cp",
                str(backup / "database.dump"),
                restored.container("postgres") + ":/tmp/database.dump",
            ],
            restored.log,
        )
        restored.compose(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "keystone",
            "-d",
            "keystone",
            "--exit-on-error",
            "--single-transaction",
            "/tmp/database.dump",
        )
        # A fresh but unrelated marker must not make old files readable.
        restored.compose("run", "--rm", "--no-deps", "init", "keystone", "init-storage")
        restored.compose("up", "-d", "--no-deps", "--wait", "api", "console")
        with restored.client() as recovered:
            recovered.cookies.update(client.cookies)
            me = request(recovered, "GET", "/auth/me")
            recovered.headers["X-CSRF-Token"] = me["csrf_token"]
            assert recovered.get(f"/api/v1/files/{file['id']}/content").status_code == 503
            restored.compose("stop", "console", "api")
            restored.archive_files(backup, restore=True)
            passed("database-only restore refuses file reads with an unrelated storage fingerprint")
            restored.up()
            assert recovered.get(f"/api/v1/files/{file['id']}/content").content == SOURCE
            assert recovered.get(f"/api/v1/files/{report}/content").content == report_bytes
            assert request(recovered, "GET", f"/flow-runs/{run['id']}") == completed
            assert request(recovered, "GET", f"/flow-runs/{run['id']}/steps") == steps
            assert request(recovered, "GET", f"/flow-runs/{run['id']}/costs") == costs
            assert request(recovered, "POST", "/flow-runs", 202, json=payload)["id"] == run["id"]
            assert (
                request(recovered, "POST", "/flow-runs", 202, json=pending_payload)["id"]
                == pending["id"]
            )
            restored.finish(recovered, pending["id"])
            current = request(recovered, "GET", "/model-invocations")
            assert len(current) == 7  # Only the restored pending run adds two calls.
            assert {x["id"] for x in invocations} <= {x["id"] for x in current}
            by_id = {x["id"]: x for x in current}
            assert all(by_id[x["id"]] == x for x in invocations)
            assert request(recovered, "GET", "/model-channels") == channels
            assert request(recovered, "GET", f"/dictionaries/{dictionary['id']}/items") == [item]
            assert request(
                recovered,
                "GET",
                "/audit-logs",
                params={"action": "flow.complete", "target_id": run["id"]},
            )
            with restored.client() as reader:
                login(reader, "reader", PASSWORD + "-changed")
                assert reader.get(f"/api/v1/files/{file['id']}/content").content == SOURCE
                assert reader.get("/api/v1/users").status_code == 403
            # Prove restored UID/permissions allow subsequent writes as well as reads.
            request(
                recovered,
                "POST",
                "/files",
                201,
                params={"name": "after-restore.txt", "expected_size": 3},
                content=b"new",
            )
            passed(
                "joint restore preserves sessions, permissions, file bytes, dictionary, "
                "costs and DBOS steps"
            )
            passed(
                "pending Flow resumes under original ID; completed calls are not replayed; "
                "writes remain usable"
            )
            restored.compose("down")  # Cold recreation with retained volumes, same images.
            restored.up()
            assert recovered.get(f"/api/v1/files/{report}/content").content == report_bytes
            assert len(request(recovered, "GET", "/model-invocations")) == 7
            passed("down/up preserves both volumes and does not replay completed model calls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build", action="store_true", help="Use locally built :verify images"
    )
    args = parser.parse_args()
    directory = ROOT / ".artifacts/deploy-verify" / uuid4().hex[:12]
    directory.mkdir(parents=True)
    print(f"Deployment artifacts: {directory}", flush=True)
    images = {}
    for name, context in (("backend", "backend"), ("console", "frontend/console")):
        tag = f"keystone-{name}:verify"
        if not args.skip_build:
            print(f"Building {name} image", flush=True)
            command(
                ["docker", "build", "-t", tag, context],
                directory / "build.log",
                timeout=900,
            )
        images[name] = command(
            ["docker", "image", "inspect", "-f", "{{.Id}}", tag],
            directory / "build.log",
        ).stdout.strip()
    postgres = "postgres:17.11-alpine"
    inspected = command(
        ["docker", "image", "inspect", "-f", "{{.Id}}", postgres],
        directory / "build.log",
        check=False,
    )
    if inspected.returncode:
        command(["docker", "pull", postgres], directory / "build.log", timeout=600)
        inspected = command(
            ["docker", "image", "inspect", "-f", "{{.Id}}", postgres], directory / "build.log"
        )
    images["postgres"] = inspected.stdout.strip()
    password = secrets.token_hex(24)
    source, restored = [Deployment(directory, images, password) for _ in range(2)]
    checks = []
    outcome = "failed"
    try:
        exercise(source, restored, checks)
        outcome = "passed"
    finally:
        # Attempt both cleanups even when the first fails, and never hide cleanup failure.
        try:
            source.cleanup()
        finally:
            restored.cleanup()
        (directory / "result.json").write_text(
            json.dumps({"status": outcome, "passed": checks, "images": images}, indent=2),
            encoding="utf-8",
        )
    print(
        f"PASS {len(checks)} deployment/recovery checks; test containers and volumes removed",
        flush=True,
    )


if __name__ == "__main__":
    main()
