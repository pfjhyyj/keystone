"""Run the console browser suite with an isolated PostgreSQL database and local API."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql

from keystone.core.config import Settings
from keystone.entrypoints.cli import initialize


def _log_tail(path, limit=2000):
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return value[-limit:]


@contextmanager
def _temporary_directory():
    resource = tempfile.TemporaryDirectory(prefix="keystone_console_")
    try:
        yield resource.name
    finally:
        # Windows may release inherited log handles just after taskkill has returned.
        # Retry sharing violations only; persistent handles and other errors still fail.
        for attempt in range(30):
            try:
                resource.cleanup()
                break
            except PermissionError as error:
                if getattr(error, "winerror", None) not in {32, 33} or attempt == 29:
                    raise
                time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playwright_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    console = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(console.parents[1] / "backend/tests"))
    from support.business_catalog import install_catalog
    from support.postgres import probe_test_database

    install_catalog()
    from support.processes import stop_process

    probe = probe_test_database()
    if not probe.available:
        raise SystemExit(f"Console E2E requires a reachable test database: {probe.detail}")
    url = probe.url
    assert url is not None
    name = "keystone_test_" + uuid4().hex
    database_url = url.set(database=name).render_as_string(hide_password=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required")
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=5,
    ) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with _temporary_directory() as directory:
                root = Path(directory) / "storage"
                credential_key = Fernet.generate_key().decode("ascii")
                settings = Settings(
                    config_file=None,
                    env="test",
                    database_url=database_url,
                    initial_admin_password="test-password-123",
                    credential_key=credential_key,
                    file_coordination_root=root,
                    file_storages={"local": {"driver": "filesystem", "root": root}},
                )
                initialize(settings, non_interactive=True)
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("KEYSTONE_")
                }
                env.update(
                    {
                        "KEYSTONE_ENV": "test",
                        "KEYSTONE_DATABASE_URL": database_url,
                        "KEYSTONE_PORT": str(port),
                        "KEYSTONE_FILE_COORDINATION_ROOT": str(root),
                        "KEYSTONE_FILE_STORAGES__LOCAL__DRIVER": "filesystem",
                        "KEYSTONE_FILE_STORAGES__LOCAL__ROOT": str(root),
                        "KEYSTONE_BROWSER_ORIGINS": json.dumps(["http://127.0.0.1:5175"]),
                        "PYTHONPATH": os.pathsep.join(
                            str(console.parents[1] / directory)
                            for directory in ("backend/tests", "examples")
                        ),
                        "KEYSTONE_E2E": "1",
                        "KEYSTONE_API_PROXY": f"http://127.0.0.1:{port}",
                    }
                )
                backend_env = env | {"KEYSTONE_CREDENTIAL_KEY": credential_key}
                api_path = Path(directory) / "api.log"
                worker_path = Path(directory) / "worker.log"
                with (
                    api_path.open("w", encoding="utf-8") as log,
                    worker_path.open("w", encoding="utf-8") as worker_log,
                ):
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "from support.model_stub import install; install(); "
                            "from support.flow_fixture import install; install(); "
                            "import keystone.entrypoints.api as api; "
                            "from support.business_api import create_app; "
                            "api.create_app = create_app; "
                            "api.main()",
                        ],
                        cwd=directory,
                        env=backend_env,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    with ExitStack() as children:
                        children.callback(stop_process, process, timeout=15)
                        worker_process = subprocess.Popen(
                            [
                                sys.executable,
                                "-c",
                                "from support.model_stub import install; install(); "
                                "from support.flow_fixture import install; install(); "
                                "from keystone.entrypoints.worker import main; main()",
                            ],
                            cwd=directory,
                            env=backend_env,
                            stdout=worker_log,
                            stderr=worker_log,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                        children.callback(stop_process, worker_process, timeout=15)
                        for _ in range(100):
                            if process.poll() is not None:
                                raise RuntimeError(
                                    "Test API stopped before readiness:\n" + _log_tail(api_path)
                                )
                            try:
                                with urllib.request.urlopen(
                                    f"http://127.0.0.1:{port}/api/v1/ready", timeout=1
                                ):
                                    break
                            except OSError:
                                time.sleep(0.1)
                        else:
                            raise RuntimeError(
                                "Test API readiness timeout:\n" + _log_tail(api_path)
                            )
                        for _ in range(300):
                            if worker_process.poll() is not None:
                                raise RuntimeError(
                                    "Test Worker stopped before readiness:\n"
                                    + _log_tail(worker_path)
                                )
                            if "flow_worker_ready" in worker_path.read_text(encoding="utf-8"):
                                break
                            time.sleep(0.1)
                        else:
                            raise RuntimeError(
                                "Test Worker readiness timeout:\n" + _log_tail(worker_path)
                            )
                        result = subprocess.run(
                            [
                                node,
                                "node_modules/@playwright/test/cli.js",
                                "test",
                                *args.playwright_args,
                            ],
                            cwd=console,
                            env=env,
                            check=False,
                        )
                return result.returncode
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    raise SystemExit(main())
