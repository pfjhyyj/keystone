"""Exercise the shipped generator through real migrations, HTTP and audit transactions."""

import asyncio
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import registry
from test_identity import (
    NEW_PASSWORD,
    PASSWORD,
    assign,
    bearer,
    create_user,
    login,
    query,
    role,
    token,
)

from keystone import bootstrap, businesses, composition, model_registry
from keystone.core import migrations
from keystone.core.asyncio import new_event_loop
from keystone.core.database import Base
from keystone.core.errors import ServiceError
from keystone.entrypoints import cli
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.identity.services.authorization import identity_lock

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[3]
NAMESPACE = "scaffold_notes"
PACKAGE = f"keystone.businesses.{NAMESPACE}"
READ, WRITE = f"{NAMESPACE}.note:read", f"{NAMESPACE}.note:write"
ACTION = f"{NAMESPACE}.note.save"
URL = f"/api/v1/{NAMESPACE}/notes"


@pytest.fixture
def business(tmp_path, monkeypatch, empty_database_url):
    directory = tmp_path / "migrations"
    shutil.copytree(
        ROOT / "backend/migrations", directory, ignore=shutil.ignore_patterns("__pycache__")
    )
    generated = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/new_business.py"),
            NAMESPACE,
            "--entity",
            "note",
            "--output",
            str(tmp_path / "businesses"),
            "--migrations",
            str(directory / "versions"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Created business" in generated.stdout
    monkeypatch.setattr(
        businesses, "__path__", [*businesses.__path__, str(tmp_path / "businesses")]
    )
    tables = set(Base.metadata.tables)
    # Keep temporary generated classes out of the platform's long-lived mapper registry.
    business_models = registry(metadata=Base.metadata)
    monkeypatch.setattr("keystone.core.database.Base", business_models.generate_base())
    original_models, original_config = model_registry.load_models, migrations.migration_config

    def load_models():
        original_models()
        importlib.import_module(f"{PACKAGE}.models")
        return Base.metadata

    def migration_config(settings):
        config = original_config(settings)
        config.set_main_option("script_location", str(directory))
        return config

    monkeypatch.setattr(model_registry, "load_models", load_models)
    monkeypatch.setattr(bootstrap, "load_models", load_models)
    monkeypatch.setattr(migrations, "migration_config", migration_config)
    monkeypatch.setattr(cli, "migration_config", migration_config)
    try:
        catalog = importlib.import_module(f"{PACKAGE}.catalog").CATALOG
        monkeypatch.setattr(composition, "BUSINESS_CATALOGS", ((NAMESPACE, catalog),))
        settings = bootstrap.Settings(
            config_file=None,
            env="test",
            database_url=empty_database_url,
            file_coordination_root=tmp_path / "files",
            file_storages={"local": {"driver": "filesystem", "root": str(tmp_path / "files")}},
            initial_admin_password=PASSWORD,
        )
        cli.initialize(settings, non_interactive=True)
        command.check(migration_config(settings))
        model = importlib.import_module(f"{PACKAGE}.models").Note
        app = bootstrap.create_app(settings)
        app.include_router(importlib.import_module(f"{PACKAGE}.controllers").router)
        with TestClient(app, backend_options={"loop_factory": new_event_loop}) as client:
            assert client.get("/api/v1/ready").status_code == 200
            login(client)
            yield SimpleNamespace(client=client, model=model)
    finally:
        business_models.dispose()
        for name in set(Base.metadata.tables) - tables:
            Base.metadata.remove(Base.metadata.tables[name])
        for name in list(sys.modules):
            if name == PACKAGE or name.startswith(PACKAGE + "."):
                del sys.modules[name]
        if hasattr(businesses, NAMESPACE):
            delattr(businesses, NAMESPACE)


def create(client, **values):
    response = client.post(URL, json={"title": "笔记", "content": "正文", **values})
    assert response.status_code == 201, response.text
    return response.json()


def logs(client, identifier):
    return client.get(
        "/api/v1/audit-logs", params={"target_id": identifier, "action": ACTION}
    ).json()


def test_generated_crud_partial_updates_and_bounded_audit(business):
    client = business.client
    content = "长正文𠮷" * 1000
    row = create(client, content=content)
    assert client.get(URL).json() == [row]
    path = f"{URL}/{row['id']}"
    renamed = client.patch(path, json={"title": "新标题"})
    assert renamed.status_code == 200 and renamed.json()["content"] == content
    edited = client.patch(path, json={"content": ""})
    assert edited.status_code == 200 and edited.json()["title"] == "新标题"
    saved = logs(client, row["id"])
    assert len(saved) == 3
    after = next(log for log in saved if "before" not in log["details"])["details"]["after"]
    assert after == {
        "title": "笔记",
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    assert all(len(json.dumps(log["details"], ensure_ascii=False).encode()) < 8192 for log in saved)
    assert content not in json.dumps(saved, ensure_ascii=False)
    before_noop = edited.json()
    assert client.patch(path, json={}).json() == before_noop
    assert len(logs(client, row["id"])) == 3
    for data in ({"title": None}, {"content": None}, {"title": ""}, {"owner_id": row["id"]}):
        assert client.patch(path, json=data).status_code == 422
    assert client.get(URL).json() == [before_noop]


def test_owner_scope_function_permissions_and_pat_intersection(business):
    client = business.client
    admin_row = create(client)
    alice = create_user(client)
    editor = role(client, "notes_editor", [READ, WRITE])
    assert assign(client, alice["id"], editor["id"]).status_code == 204
    login(client, "alice")
    assert (
        client.post(
            "/api/v1/auth/password",
            json={
                "current_password": PASSWORD,
                "password": NEW_PASSWORD,
            },
        ).status_code
        == 204
    )
    login(client, "alice", NEW_PASSWORD)
    assert client.get(URL).json() == []
    assert client.patch(f"{URL}/{admin_row['id']}", json={"title": "stolen"}).status_code == 404
    own = create(client)
    assert own["owner_id"] == alice["id"]
    reader = token(client, [READ])
    auth = bearer(reader["token"])
    assert client.get(URL, headers=auth).json() == [own]
    assert client.post(URL, headers=auth, json={"title": "denied"}).status_code == 403
    assert (
        client.patch(f"{URL}/{own['id']}", headers=auth, json={"title": "denied"}).status_code
        == 403
    )
    writer = token(client, [WRITE])
    auth = bearer(writer["token"])
    assert client.get(URL, headers=auth).status_code == 403
    assert (
        client.patch(f"{URL}/{admin_row['id']}", headers=auth, json={"title": "stolen"}).status_code
        == 404
    )
    updated = client.patch(f"{URL}/{own['id']}", headers=auth, json={"content": "changed"})
    assert updated.status_code == 200
    login(client)
    assert client.get(URL).json() == [admin_row]
    assert client.patch(f"{URL}/{own['id']}", json={"title": "admin bypass"}).status_code == 404
    assert any(
        log["details"].get("credential_id") == writer["id"] for log in logs(client, own["id"])
    )
    assert (
        client.put(
            f"/api/v1/roles/{editor['id']}", json={"name": "notes_editor", "permissions": [READ]}
        ).status_code
        == 200
    )
    assert (
        client.patch(f"{URL}/{own['id']}", headers=auth, json={"title": "revoked"}).status_code
        == 403
    )
    denied = next(
        log
        for log in logs(client, own["id"])
        if log["details"].get("credential_id") == writer["id"] and log["outcome"] == "denied"
    )
    assert denied["actor_id"] == alice["id"]


@pytest.mark.parametrize("editing", [False, True])
def test_generated_audit_failure_rolls_back_domain_write(business, monkeypatch, editing):
    client, model = business.client, business.model
    row = create(client) if editing else None
    original = audit.record

    async def reject(session, action, *args, **kwargs):
        if action == ACTION and kwargs.get("outcome", "success") == "success":
            raise ServiceError(409, "test_rejected", "Injected audit failure")
        return await original(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", reject)
    response = (
        client.patch(f"{URL}/{row['id']}", json={"title": "rollback"})
        if editing
        else client.post(URL, json={"title": "rollback"})
    )
    assert response.status_code == 409

    async def facts(session):
        assert await session.scalar(select(func.count()).select_from(model)) == (
            1 if editing else 0
        )
        if editing:
            assert (await session.get(model, UUID(row["id"]))).title == "笔记"
        outcomes = list(
            await session.scalars(select(AuditLog.outcome).where(AuditLog.action == ACTION))
        )
        assert outcomes.count("success") == (1 if editing else 0)
        assert outcomes.count("failure") == 1

    query(client, facts)


def test_business_writes_share_identity_lock(business):
    client = business.client

    async def exercise():
        async with client.app.state.runtime.database.transaction() as session:
            await identity_lock(session)
            response = await asyncio.to_thread(client.post, URL, json={"title": "concurrent"})
            assert response.status_code == 201, response.text
            edited = await asyncio.to_thread(
                client.patch, f"{URL}/{response.json()['id']}", json={"content": "updated"}
            )
            assert edited.status_code == 200, edited.text

    client.portal.call(exercise)
