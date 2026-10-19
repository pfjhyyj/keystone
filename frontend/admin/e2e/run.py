"""Run the admin browser suite with an isolated PostgreSQL database and local API."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from dbos import DBOS
from psycopg import sql
from sqlalchemy.engine import make_url

from keystone.composition import build_catalog
from keystone.core.asyncio import run
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.migrations import migration_config
from keystone.platform.files.leases import initialize
from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferences
from keystone.platform.files.storage import StorageRegistry
from keystone.platform.identity.access import AccessContext
from keystone.platform.identity.policy import Credential
from keystone.platform.identity.schemas import Login, UserCreate
from keystone.platform.identity.security import Passwords
from keystone.platform.identity.services.auth import AuthService
from keystone.platform.identity.services.initialize import initialize_admin


def main():
    url = make_url(os.environ["KEYSTONE_TEST_DATABASE_URL"])
    if url.database != "keystone_test":
        raise RuntimeError("Only the dedicated keystone_test database is accepted")
    admin = Path(__file__).resolve().parents[1]
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
        process = None
        worker_process = None
        try:
            with tempfile.TemporaryDirectory(prefix="keystone_admin_") as directory:
                root = Path(directory) / "storage"
                initialize(root)
                settings = Settings(
                    _env_file=None,
                    env="test",
                    database_url=database_url,
                    file_coordination_root=root,
                    file_storages={"local": {"driver": "filesystem", "root": root}},
                )
                config = migration_config(settings)
                config.attributes["create_schema"] = True
                command.upgrade(config, "head")
                DBOS.migrate(database_url, schema="dbos")

                async def seed():
                    database = Database(settings, catalog=build_catalog())
                    try:
                        await initialize_admin(
                            database,
                            Passwords(),
                            UserCreate(
                                username="admin",
                                display_name="管理员",
                                password="test-password-123",
                            ),
                        )
                        # A trusted business fixture exercises read-only references and
                        # deletion protection without adding a public reference write API.
                        identity = AccessContext(database, settings)
                        secret, _ = await AuthService(identity, Passwords()).login(
                            Login(username="admin", password="test-password-123")
                        )
                        identity.credential = Credential("session", secret)
                        stored = await FileService(identity, StorageRegistry(settings)).upload(
                            Upload(name="referenced.txt", expected_size=7), b"fixture"
                        )
                        async with identity.access(write=True) as (session, auth):
                            await FileReferences(session, auth, "e2e").replace(
                                "record:1", "input", [stored["id"]]
                            )
                    finally:
                        await database.close()

                run(seed())
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
                        "KEYSTONE_FILE_STORAGES": json.dumps(
                            {"local": {"driver": "filesystem", "root": str(root)}}
                        ),
                        "KEYSTONE_BROWSER_ORIGINS": json.dumps(["http://127.0.0.1:5175"]),
                        "KEYSTONE_MODEL_MODE": "mock",
                        "KEYSTONE_MODEL_SECRETS": json.dumps(
                            {"inference": "fake-inference", "billing": "fake-billing"}
                        ),
                        "KEYSTONE_FLOW_ENABLED": "true",
                        "KEYSTONE_E2E": "1",
                        "KEYSTONE_API_PROXY": f"http://127.0.0.1:{port}",
                    }
                )
                worker_path = Path(directory) / "worker.log"
                with (
                    (Path(directory) / "api.log").open("w", encoding="utf-8") as log,
                    worker_path.open("w", encoding="utf-8") as worker_log,
                ):
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "from keystone.entrypoints.api import main; main()",
                        ],
                        cwd=directory,
                        env=env,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    try:
                        worker_process = subprocess.Popen(
                            [
                                sys.executable,
                                "-c",
                                "from keystone.entrypoints.worker import main; main()",
                            ],
                            cwd=directory,
                            env=env,
                            stdout=worker_log,
                            stderr=worker_log,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                        for _ in range(100):
                            if process.poll() is not None:
                                raise RuntimeError("Test API stopped before readiness")
                            try:
                                with urllib.request.urlopen(
                                    f"http://127.0.0.1:{port}/api/v1/ready", timeout=1
                                ):
                                    break
                            except OSError:
                                time.sleep(0.1)
                        else:
                            raise RuntimeError("Test API readiness timeout")
                        for _ in range(300):
                            if worker_process.poll() is not None:
                                raise RuntimeError("Test Worker stopped before readiness")
                            if "flow_worker_ready" in worker_path.read_text(encoding="utf-8"):
                                break
                            time.sleep(0.1)
                        else:
                            raise RuntimeError("Test Worker readiness timeout")
                        result = subprocess.run(
                            [node, "node_modules/@playwright/test/cli.js", "test"],
                            cwd=admin,
                            env=env,
                            check=False,
                        )
                    finally:
                        for child in (worker_process, process):
                            if child is None:
                                continue
                            if os.name == "nt":
                                subprocess.run(
                                    ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                    check=False,
                                    capture_output=True,
                                )
                            else:
                                child.terminate()
                            child.wait(timeout=15)
                return result.returncode
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    raise SystemExit(main())
