"""Operations use real credentials, native DBOS history and per-domain facts."""

from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

import pytest
from dbos import DBOSClient
from sqlalchemy import func, select, text, update
from support.flow_engine import flow_engine as flow_engine
from test_files_dictionary import upload
from test_flow import client as client
from test_flow import configuration as configuration
from test_flow import start, worker
from test_identity import bearer, query, token
from test_maintenance import schedules, wait_for

from keystone.core.execution.history import prune_maintenance
from keystone.core.time import utcnow
from keystone.platform.audit.models import AuditLog
from keystone.platform.files.models import File
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.services.identity import retained_execution_ids
from keystone.platform.identity.models import LoginSession, PersonalAccessToken
from keystone.platform.model_access.models import ModelEntry, ModelInvocation

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("flow_engine")]


def test_history_retention_never_deletes_another_application_or_its_parent(client):
    runtime = client.app.state.runtime
    engines = [
        DBOSClient(
            system_database_url=runtime.settings.database_url.get_secret_value(),
            application_name=name,
            lazy=True,
        )
        for name in ("keystone", "another-application")
    ]
    old = int((utcnow() - timedelta(days=40)).timestamp() * 1000)
    try:
        for engine, queue, ids in (
            (
                engines[0],
                "owned-retention",
                ("owned-parent", "owned-leaf", "owned-under-foreign", "unclaimed"),
            ),
            (engines[1], "foreign-retention", ("foreign-root", "foreign-child")),
        ):
            engine.register_queue(queue)
            for identifier in ids:
                engine.enqueue(
                    {
                        "workflow_name": "keystone.files.cleanup_pass",
                        "queue_name": queue,
                        "workflow_id": identifier,
                    },
                    {},
                )
                engine.cancel_workflow(identifier)

        async def age(session):
            await session.execute(
                text("UPDATE dbos.workflow_status SET completed_at = :old"), {"old": old}
            )
            await session.execute(
                text(
                    "UPDATE dbos.workflow_status SET parent_workflow_id = 'owned-parent' "
                    "WHERE workflow_uuid = 'foreign-child'"
                )
            )
            await session.execute(
                text(
                    "UPDATE dbos.workflow_status SET parent_workflow_id = 'foreign-root' "
                    "WHERE workflow_uuid = 'owned-under-foreign'"
                )
            )
            await session.execute(
                text(
                    "UPDATE dbos.workflow_status SET application_name = NULL "
                    "WHERE workflow_uuid = 'unclaimed'"
                )
            )

        query(client, age)
        client.portal.call(partial(runtime.history_retention().execute, apply=True))
        remaining = {
            row.workflow_id
            for row in engines[0].list_workflows(
                load_input=False, load_output=False, application_name=[]
            )
        }
        assert remaining == {
            "owned-parent",
            "foreign-root",
            "foreign-child",
            "owned-under-foreign",
            "unclaimed",
        }
    finally:
        for engine in engines:
            engine.destroy()


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_history_cleanup_is_explicitly_enabled(client, tmp_path, enabled):
    runtime = client.app.state.runtime
    runtime.settings.history_cleanup_enabled = enabled

    async def seed(session):
        session.add(
            AuditLog(
                occurred_at=utcnow() - timedelta(days=400),
                actor_type="system",
                action="retention.worker_probe",
                outcome="success",
            )
        )

    query(client, seed)

    def count():
        return query(
            client,
            lambda session: session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "retention.worker_probe")
            ),
        )

    with worker(client, tmp_path) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: client.get("/api/v1/worker/status").json()["status"] == "ready",
        )
        if enabled:
            wait_for(client, process, log, lambda: count() == 0)
        else:
            assert count() == 1
        assert ("keystone.history.retention" in schedules(client)) is enabled


def test_status_requires_current_identity_and_reports_backlog(client):
    personal = token(client, [])
    response = client.get("/api/v1/worker/status", headers=bearer(personal["token"]))
    assert response.status_code == 200
    assert response.json()["backlog"]["flow"]["pending"] == 0
    file_id = upload(client).json()["id"]
    run = start(client, file_id).json()
    old = utcnow() - timedelta(hours=2)

    async def seed(session):
        await session.execute(
            update(FlowRun).where(FlowRun.id == UUID(run["id"])).values(created_at=old)
        )
        await session.execute(
            update(File).where(File.id == UUID(file_id)).values(status="deleting", updated_at=old)
        )
        entry = await session.scalar(select(ModelEntry))
        session.add(
            ModelInvocation(
                model_entry_id=entry.id,
                channel_id=entry.channel_id,
                provider_model=entry.provider_model,
                status="unknown",
                finished_at=old,
                cost_check_status="pending",
                cost_next_check_at=old,
            )
        )

    query(client, seed)
    backlog = client.get("/api/v1/worker/status").json()["backlog"]
    assert set(backlog) == {"flow", "files", "model_costs"}
    assert all(
        item["pending"] == 1 and item["oldest_pending_age_seconds"] >= 7200
        for item in backlog.values()
    )
    assert client.delete(f"/api/v1/auth/tokens/{personal['id']}").status_code == 204
    assert client.get("/api/v1/worker/status", headers=bearer(personal["token"])).status_code == 401
    client.cookies.clear()
    unauthenticated = client.get("/api/v1/worker/status")
    assert unauthenticated.status_code == 401 and "instances" not in unauthenticated.json()
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/ready").status_code == 200


def test_retention_preserves_active_recent_and_run_linked_credentials_and_calls(client):
    runtime = client.app.state.runtime
    assert runtime.settings.history_cleanup_enabled is False
    admin_id = UUID(client.get("/api/v1/auth/me").json()["user"]["id"])
    personal = token(client, client.get("/api/v1/auth/me").json()["permissions"])
    file_id = upload(client).json()["id"]
    response = client.post(
        "/api/v1/flow-runs",
        headers=bearer(personal["token"]),
        json={
            "definition_key": "document.review",
            "request_key": "retained-pat",
            "input": {"source_file_id": file_id},
            "model_bindings": {"review": "chat"},
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    old = utcnow() - timedelta(days=400)
    recent = utcnow() - timedelta(days=1)
    expected_deleted = []
    kept = []

    async def seed(session):
        for model in (LoginSession, PersonalAccessToken):
            for expiry, revoked, keep in (
                (old, None, False),
                (utcnow() + timedelta(days=1), old, False),
                (recent, None, True),
                (utcnow() + timedelta(days=1), None, True),
            ):
                identifier = uuid4()
                values = dict(
                    id=identifier,
                    user_id=admin_id,
                    token_digest=uuid4().hex * 2,
                    expires_at=expiry,
                    revoked_at=revoked,
                )
                if model is PersonalAccessToken:
                    values.update(name="retention", permissions=[])
                session.add(model(**values))
                (kept if keep else expected_deleted).append((model, identifier))
        await session.execute(
            update(PersonalAccessToken)
            .where(PersonalAccessToken.id == UUID(personal["id"]))
            .values(expires_at=old)
        )
        await session.execute(
            update(FlowRun).where(FlowRun.id == run_id).values(status="failed", finished_at=old)
        )
        session.add(
            AuditLog(occurred_at=old, actor_type="system", action="old.probe", outcome="success")
        )
        entry = await session.scalar(select(ModelEntry))
        session.add(
            ModelInvocation(
                model_entry_id=entry.id,
                channel_id=entry.channel_id,
                provider_model=entry.provider_model,
                status="unknown",
                finished_at=old,
                created_at=old,
                cost_check_status="pending",
                cost_next_check_at=old,
            )
        )

    query(client, seed)
    preview = client.portal.call(runtime.history_retention().execute)
    assert preview["sessions"] == preview["tokens"] == 2 and preview["audit"] == 1
    assert preview["applied"] is False
    assert (
        query(
            client,
            lambda session: session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "old.probe")
            ),
        )
        == 1
    )
    applied = client.portal.call(partial(runtime.history_retention().execute, apply=True))
    assert applied["sessions"] == applied["tokens"] == 2 and applied["applied"] is True

    async def verify(session):
        for model, identifier in expected_deleted:
            assert await session.get(model, identifier) is None
        for model, identifier in kept:
            assert await session.get(model, identifier) is not None
        assert await session.get(PersonalAccessToken, UUID(personal["id"])) is not None
        assert await session.get(FlowRun, run_id) is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ModelInvocation)
                .where(ModelInvocation.status == "unknown")
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "old.probe")
            )
            == 0
        )

    query(client, verify)
    repeated = client.portal.call(partial(runtime.history_retention().execute, apply=True))
    assert repeated["sessions"] == repeated["tokens"] == repeated["audit"] == 0


def test_native_history_retention_preserves_business_active_recent_and_parent_facts(client):
    runtime = client.app.state.runtime
    old = int((utcnow() - timedelta(days=40)).timestamp() * 1000)
    engine = DBOSClient(
        system_database_url=runtime.settings.database_url.get_secret_value(),
        application_name="keystone",
        lazy=True,
    )
    names = {
        key: "keystone.files.cleanup_pass"
        for key in (
            "old",
            "parent",
            "child",
            "active-parent",
            "active-child",
            "recent",
            "protected",
            "forked",
        )
    }
    file_id = upload(client).json()["id"]
    run = start(client, file_id).json()
    names[run["id"]] = "keystone.files.cleanup_pass"
    names["business"] = "business.actual_workflow"
    try:
        engine.register_queue("retention-fixture")
        for identifier, name in names.items():
            engine.enqueue(
                {
                    "workflow_name": name,
                    "queue_name": "retention-fixture",
                    "workflow_id": identifier,
                },
                {},
            )
            if identifier != "active-parent":
                engine.cancel_workflow(identifier)

        async def age(session):
            await session.execute(
                text(
                    "UPDATE dbos.workflow_status SET completed_at = :old "
                    "WHERE workflow_uuid != 'recent' AND status = 'CANCELLED'"
                ),
                {"old": old},
            )
            for child, parent in (("child", "parent"), ("active-child", "active-parent")):
                await session.execute(
                    text(
                        "UPDATE dbos.workflow_status SET parent_workflow_id = :parent "
                        "WHERE workflow_uuid = :child"
                    ),
                    {"parent": parent, "child": child},
                )
            await session.execute(
                text(
                    "UPDATE dbos.workflow_status SET was_forked_from = true "
                    "WHERE workflow_uuid = 'forked'"
                )
            )

        query(client, age)

        async def order(session):
            await session.execute(
                text(
                    "UPDATE dbos.workflow_status SET created_at = :old "
                    "WHERE workflow_uuid = 'parent'"
                ),
                {"old": old - 1000},
            )

        query(client, order)
        first_page = client.portal.call(
            partial(
                prune_maintenance,
                runtime.settings,
                {"keystone.files.cleanup_pass"},
                partial(retained_execution_ids, runtime.database),
                apply=True,
                limit=1,
            )
        )
        assert first_page["eligible"] == 0 and first_page["next_offset"] == 1
        following = client.portal.call(
            partial(
                prune_maintenance,
                runtime.settings,
                {"keystone.files.cleanup_pass"},
                partial(retained_execution_ids, runtime.database),
                offset=first_page["next_offset"],
            )
        )
        assert following["examined"] > 0
        preview = client.portal.call(runtime.history_retention().execute)
        assert preview["maintenance"]["eligible"] == 3  # old, unreferenced protected, child
        client.portal.call(partial(runtime.history_retention().execute, apply=True))
        remaining = {
            row.workflow_id for row in engine.list_workflows(load_input=False, load_output=False)
        }
        assert {
            "business",
            "active-parent",
            "active-child",
            "recent",
            "parent",
            "forked",
            run["id"],
        } <= remaining
        assert not {"old", "protected", "child"} & remaining
        client.portal.call(partial(runtime.history_retention().execute, apply=True))
        remaining = {
            row.workflow_id for row in engine.list_workflows(load_input=False, load_output=False)
        }
        assert "parent" not in remaining and run["id"] in remaining
    finally:
        engine.destroy()
