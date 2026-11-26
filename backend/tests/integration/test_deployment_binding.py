"""Exercise deployment claims against isolated PostgreSQL databases."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text
from support.storage_support import cloud_config
from support.storage_support import s3 as s3
from test_flow import worker
from test_maintenance import wait_for

from keystone.bootstrap import build_runtime, create_app
from keystone.core import deployment
from keystone.core.asyncio import new_event_loop
from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.core.migrations import migration_config
from keystone.entrypoints.cli import initialize
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.worker.health import worker_status
from keystone.worker.models import WorkerRegistration

pytestmark = pytest.mark.postgres


def filesystem(database_url, root):
    return Settings(
        config_file=None,
        database_url=database_url,
        file_coordination_root=root,
        file_storages={"local": {"driver": "filesystem", "root": root}},
    )


def shared(database_url, root, key=None):
    return Settings(
        config_file=None,
        database_url=database_url,
        credential_key=key or Fernet.generate_key().decode("ascii"),
        file_coordination_root=root,
        file_default_storage="cloud",
        file_storages={
            "cloud": {
                "driver": "s3",
                "endpoint": "http://127.0.0.1:1",
                "bucket": "test-bucket",
                "access_key": "test-access",
                "secret_key": "test-secret",
            }
        },
    )


def upgrade(settings, revision="heads"):
    config = migration_config(settings)
    config.attributes["create_schema"] = True
    command.upgrade(config, revision)
    return config


async def binding(runtime):
    async with runtime.database.transaction() as session:
        result = await session.execute(text("SELECT * FROM keystone.deployment_binding"))
        return result.mappings().one_or_none()


async def test_concurrent_filesystem_claims_allow_only_one_coordination_root(
    empty_database_url, tmp_path, monkeypatch
):
    monkeypatch.setattr(deployment, "gethostname", lambda: "host-one")
    one = build_runtime(filesystem(empty_database_url, tmp_path / "one"))
    two = build_runtime(filesystem(empty_database_url, tmp_path / "two"))
    cloud = build_runtime(shared(empty_database_url, tmp_path / "cloud"))
    upgrade(one.settings)
    runtimes = (one, two, cloud)
    for runtime in runtimes:
        runtime.storage.initialize()
    try:
        assert (await one.readiness())["status"] == "unavailable"
        assert await binding(one) is None
        outcomes = await asyncio.gather(
            one.bind_deployment(), two.bind_deployment(), return_exceptions=True
        )
        assert outcomes.count(None) == 1
        winner, loser = (one, two) if outcomes[0] is None else (two, one)
        failure = next(outcome for outcome in outcomes if outcome is not None)
        assert isinstance(failure, ServiceError) and failure.code == "deployment_binding_conflict"
        expected = {
            "id": 1,
            "topology": "filesystem",
            "host_id": "host-one",
            "coordination_id": winner.storage.coordination.fingerprint(),
            "credential_key_fingerprint": None,
        }
        assert await binding(loser) == expected
        await winner.bind_deployment()
        assert (await winner.readiness())["status"] == "ready"
        assert (await loser.readiness())["status"] == "unavailable"
        with pytest.raises(ServiceError) as switch:
            await cloud.bind_deployment()
        assert switch.value.code == "deployment_binding_conflict"
        # Even with the correct marker, another hostname cannot use this database.
        monkeypatch.setattr(deployment, "gethostname", lambda: "host-two")
        with pytest.raises(ServiceError) as rejected:
            await winner.require_deployment()
        assert rejected.value.code == "deployment_binding_conflict"
        assert await binding(winner) == expected
    finally:
        for runtime in runtimes:
            await runtime.close()


async def test_shared_claim_ignores_host_and_scratch_but_requires_same_vault_key(
    empty_database_url, tmp_path, monkeypatch
):
    first = build_runtime(shared(empty_database_url, tmp_path / "one"))
    second = build_runtime(
        shared(empty_database_url, tmp_path / "two", first.settings.credential_key)
    )
    wrong = build_runtime(shared(empty_database_url, tmp_path / "wrong"))
    local = filesystem(empty_database_url, tmp_path / "local")
    # Mixed deployments keep filesystem binding even when S3 is the default.
    local.file_storages |= first.settings.file_storages
    local.file_default_storage = "cloud"
    mixed = build_runtime(local)
    upgrade(first.settings)
    runtimes = (first, second, wrong, mixed)
    for runtime in runtimes:
        runtime.storage.initialize()
    try:
        monkeypatch.setattr(deployment, "gethostname", lambda: "host-one")
        await first.bind_deployment()
        monkeypatch.setattr(deployment, "gethostname", lambda: "host-two")
        assert first.storage.coordination.fingerprint() != second.storage.coordination.fingerprint()
        await second.require_deployment()
        row = await binding(first)
        assert row["topology"] == "shared"
        assert row["host_id"] is None and row["coordination_id"] is None
        assert len(row["credential_key_fingerprint"]) == 64
        assert first.settings.credential_key.get_secret_value() not in str(dict(row))
        with pytest.raises(ServiceError) as rejected:
            await wrong.require_deployment()
        assert rejected.value.code == "deployment_binding_conflict"
        assert "KEYSTONE_CREDENTIAL_KEY" in rejected.value.detail
        with pytest.raises(ServiceError) as switch:
            await mixed.bind_deployment()
        assert switch.value.code == "deployment_binding_conflict"
        assert await binding(first) == row
        owner = uuid4()
        ciphertext = first.messaging_vault().encrypt(
            SecretStr("test-value"), owner, "test-messaging"
        )
        assert second.messaging_vault().decrypt(ciphertext, owner, "test-messaging") == "test-value"
    finally:
        for runtime in runtimes:
            await runtime.close()


def test_api_requires_explicit_binding_and_recovers_without_restart(empty_database_url, tmp_path):
    settings = filesystem(empty_database_url, tmp_path / "node")
    upgrade(settings, "0010")
    loop = {"loop_factory": new_event_loop}

    async def bind():
        runtime = build_runtime(settings)
        try:
            await runtime.bind_deployment()
        finally:
            await runtime.close()

    with TestClient(create_app(settings), backend_options=loop) as client:
        assert client.get("/api/v1/health").status_code == 200
        ready = client.get("/api/v1/ready")
        assert ready.status_code == 503 and ready.json()["checks"]["schema"] == "out_of_date"
        upgrade(settings)
        with pytest.raises(ServiceError) as missing:
            client.portal.call(bind)
        assert missing.value.code == "storage_unavailable"
        StorageRegistry(settings).initialize()
        runtime = client.app.state.runtime
        assert client.get("/api/v1/ready").status_code == 503
        assert client.get("/api/v1/users").json()["code"] == "deployment_not_bound"
        assert client.portal.call(binding, runtime) is None
        client.portal.call(bind)
        assert client.get("/api/v1/ready").status_code == 200
        assert client.get("/api/v1/users").status_code == 401

    wrong = filesystem(empty_database_url, tmp_path / "wrong")
    StorageRegistry(wrong).initialize()
    with TestClient(create_app(wrong), backend_options=loop) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 503
        assert client.get("/api/v1/users").json()["code"] == "deployment_binding_conflict"


async def test_first_claim_respects_existing_file_locations_and_worker_evidence(
    empty_database_url, tmp_path, monkeypatch
):
    monkeypatch.setattr(deployment, "gethostname", lambda: "legacy-host")
    local = build_runtime(filesystem(empty_database_url, tmp_path / "local"))
    cloud = build_runtime(shared(empty_database_url, tmp_path / "cloud"))
    config = upgrade(cloud.settings, "0010")
    local.storage.initialize()
    cloud.storage.initialize()
    source = cloud.storage.get()
    fingerprints = {"local": local.storage.get().fingerprint(), "cloud": source.fingerprint()}
    old_marker = "0" * 64
    try:
        async with cloud.database.transaction() as session:
            owner = uuid4()
            await session.execute(
                text("""
                    INSERT INTO keystone.users
                    (id, username, display_name, password_hash, is_active, password_change_required)
                    VALUES (:owner, 'legacy', 'Legacy', 'unused', true, false)
                """),
                {"owner": owner},
            )
            await session.execute(
                text("""
                    INSERT INTO keystone.files
                    (id, name, owner_id, content_type, size_bytes, checksum_sha256, upload_key,
                     upload_digest, storage_key, storage_fingerprint, object_key, status)
                    VALUES (:id, :key, :owner, 'text/plain', 1, repeat('a', 64), :key,
                            repeat('a', 64), :key, :fingerprint, :key, 'active')
                """),
                [
                    {"id": uuid4(), "owner": owner, "key": key, "fingerprint": fingerprint}
                    for key, fingerprint in fingerprints.items()
                ],
            )
            await session.execute(
                text("""
                    INSERT INTO keystone.worker_registrations (worker_id, host_id, coordination_id)
                    VALUES ('legacy', 'legacy-host', :marker)
                """),
                {"marker": old_marker},
            )
        command.upgrade(config, "heads")
        # Wrong filesystem root and an S3 claim that would strand local content both fail.
        for runtime in (local, cloud):
            with pytest.raises(ServiceError) as rejected:
                await runtime.bind_deployment()
            assert rejected.value.code == "deployment_binding_conflict"
        async with cloud.database.transaction() as session:
            await session.execute(
                text("""
                    UPDATE keystone.files SET status = 'deleted', deleted_at = now()
                    WHERE storage_key = 'local'
                """)
            )
        with monkeypatch.context() as changed:
            changed.setattr(source.config, "bucket", "other-bucket")
            with pytest.raises(ServiceError) as retargeted:
                await cloud.bind_deployment()
            assert retargeted.value.code == "deployment_binding_conflict"
        assert await binding(cloud) is None

        # The old Worker row cannot veto shared storage and the claim does not rewrite locations.
        await cloud.bind_deployment()
        assert (await binding(cloud))["topology"] == "shared"
        async with cloud.database.transaction() as session:
            marker = await session.scalar(
                select(WorkerRegistration.coordination_id).where(
                    WorkerRegistration.worker_id == "legacy"
                )
            )
            assert marker == old_marker
            result = await session.execute(
                text(
                    "SELECT storage_key, storage_fingerprint FROM keystone.files "
                    "WHERE status = 'active'"
                )
            )
            assert dict(result.tuples().all()) == {"cloud": fingerprints["cloud"]}
    finally:
        await local.close()
        await cloud.close()


def test_s3_workers_use_one_database_with_distinct_hosts_and_node_disks(
    empty_database_url, tmp_path, s3
):
    settings = Settings(
        config_file=None,
        database_url=empty_database_url,
        initial_admin_password="isolated-admin-password",
        credential_key=Fernet.generate_key().decode("ascii"),
        file_coordination_root=tmp_path / "api-disk",
        file_default_storage="cloud",
        file_storages={"cloud": cloud_config(s3)},
    )
    initialize(settings, non_interactive=True)

    def node(name):
        return f"""
import os, socket
from keystone.core.config import Settings
from keystone.entrypoints.worker import main
from keystone.platform.files.adapters.storage import StorageRegistry
socket.gethostname = lambda: {name!r}
os.environ['KEYSTONE_FILE_COORDINATION_ROOT'] = {str(tmp_path / name)!r}
StorageRegistry(Settings()).initialize()
main()
"""

    with TestClient(create_app(settings), backend_options={"loop_factory": new_event_loop}) as api:
        runtime = api.app.state.runtime

        async def ready_hosts():
            status = await worker_status(
                runtime.database,
                settings=settings,
                application_version=runtime.execution_version(),
                process_only=True,
            )
            ready = {row["worker_id"] for row in status["instances"] if row["status"] == "ready"}
            async with runtime.database.transaction() as session:
                rows = await session.scalars(select(WorkerRegistration))
                return {row.worker_id: row.host_id for row in rows if row.worker_id in ready}

        peer = "keystone-worker-02"
        with worker(api, tmp_path, code=node("node-b"), stub=False, worker_id=peer) as (
            second,
            second_log,
        ):
            with worker(api, tmp_path, code=node("node-a"), stub=False) as (first, log):
                wait_for(
                    api,
                    first,
                    log,
                    lambda: (
                        api.portal.call(ready_hosts)
                        == {settings.worker_id: "node-a", peer: "node-b"}
                    ),
                )
                with worker(api, tmp_path, code=node("duplicate"), stub=False) as (duplicate, log):
                    assert duplicate.wait(timeout=15) == 1
                    assert "worker_identity_busy" in log.read_text(encoding="utf8")

            with worker(api, tmp_path, code=node("replacement"), stub=False) as (replacement, log):
                wait_for(
                    api,
                    replacement,
                    log,
                    lambda: (
                        api.portal.call(ready_hosts)
                        == {settings.worker_id: "replacement", peer: "node-b"}
                    ),
                )
                assert second.poll() is None, second_log.read_text(encoding="utf8")
