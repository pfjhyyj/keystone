"""Public batch reads and business-owned deletion on real PostgreSQL."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from support.flow_admission import accept
from test_files_dictionary import client as client
from test_identity import create_user, query, role
from test_managed_files import WRITE, business
from test_managed_files import notes as notes
from test_model_access import channel, entry

from keystone.core.time import utcnow
from keystone.platform.dictionary.services.dictionaries import describe_items_in_transaction
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services.references import FileReferences
from keystone.platform.flow.schemas import RunOutcome
from keystone.platform.flow.tasks.workflows import reconcile_one
from keystone.platform.identity.services.subjects import role_in_transaction, users_in_transaction
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


def test_dictionary_batch_is_scoped_and_reports_current_inactive_state(client):
    ids = []
    for key in ("category", "other"):
        parent = client.post("/api/v1/dictionaries", json={"key": key, "name": key}).json()
        item = client.post(
            f"/api/v1/dictionaries/{parent['id']}/items", json={"value": "one", "label": key}
        ).json()
        ids.append(UUID(item["id"]))
        client.patch(f"/api/v1/dictionaries/{parent['id']}", json={"name": key, "is_active": False})
    rows = query(client, lambda s: describe_items_in_transaction(s, ["category"], ids))
    assert [row["id"] for row in rows] == ids[:1]
    assert rows[0]["dictionary_is_active"] is False
    assert rows[0]["is_active"] is True


def test_subject_lookup_returns_only_public_fields_and_current_status(client):
    user = create_user(client)
    assigned = role(client, "reader", [])
    client.put(f"/api/v1/users/{user['id']}/active", json={"is_active": False})
    users = query(client, lambda s: users_in_transaction(s, [UUID(user["id"])]))
    assert users == [{"id": UUID(user["id"]), "label": "alice", "is_active": False}]
    assert query(client, lambda s: role_in_transaction(s, UUID(assigned["id"]))) == {
        "id": UUID(assigned["id"]),
        "key": "reader",
    }


def test_batch_run_statuses_do_not_return_details(client):
    first, _ = accept(client, request_key="one")
    second, _ = accept(client, request_key="two")
    ids = [UUID(row.json()["id"]) for row in (first, second)]
    flow = client.app.state.runtime.flow_service(None)
    assert query(client, lambda s: flow.statuses_in_transaction(s, ids)) == dict.fromkeys(
        ids, "pending"
    )


def test_batch_run_outcomes_separate_known_failure_unknown_result_cancel_and_timeout(client):
    engine_states = {
        "succeeded": {"status": "SUCCESS", "result": {}},
        "known": {"status": "ERROR", "error_code": "model_invalid_request"},
        "sent": {"status": "ERROR", "error_code": "model_timeout"},
        "registered": {"status": "ERROR", "error_code": "model_operation_unavailable"},
        "lost": {"status": "ERROR", "error_code": "model_result_unavailable"},
        "cancelled": {"status": "CANCELLED", "timed_out": True},
        "timeout": {"status": "CANCELLED", "timed_out": True},
        "pending": None,
    }
    runs = {}
    for key in engine_states:
        response, _ = accept(client, request_key=key)
        runs[key] = response.json()
    ids = {key: UUID(row["id"]) for key, row in runs.items()}
    assert client.post(f"/api/v1/flow-runs/{ids['cancelled']}/cancel").status_code == 200

    configured = entry(client, channel(client))
    calls = {
        "sent": "unknown",
        "registered": "running",
        "lost": "succeeded",
        "succeeded": "unknown",
    }

    async def record(session):
        for key, status in calls.items():
            session.add(
                ModelInvocation(
                    user_id=UUID(runs[key]["actor_id"]),
                    model_entry_id=UUID(configured["id"]),
                    channel_id=UUID(configured["channel_id"]),
                    provider_model="mock-chat",
                    operation="text.generate",
                    status=status,
                    finished_at=None if status == "running" else utcnow(),
                    error_code="private-provider-text" if status == "unknown" else None,
                    flow_context={"flow_run_id": str(ids[key]), "model_role": "review"},
                )
            )

    query(client, record)

    class Engine:
        async def state(self, identifier):
            key = next(key for key, value in ids.items() if value == identifier)
            if engine_states[key] is None:
                return None
            return {
                "version": runs[key]["execution_version"],
                "name": "test.resources.run",
                "result": None,
                "error_code": None,
                "timed_out": False,
                **engine_states[key],
            }

        async def enqueue(self, run):
            pass

    for key in engine_states:
        if engine_states[key] is not None:
            client.portal.call(reconcile_one, client.app.state.runtime, Engine(), ids[key])

    flow = client.app.state.runtime.flow_service(None)
    unrelated = uuid4()
    outcomes = query(client, lambda s: flow.outcomes_in_transaction(s, [*ids.values(), unrelated]))
    assert outcomes == {
        ids["succeeded"]: RunOutcome(status="succeeded"),
        ids["known"]: RunOutcome(status="failed", error_code="model_invalid_request"),
        # A call recorded as unknown, or registered and never finished before a crash.
        ids["sent"]: RunOutcome(
            status="failed", error_code="model_timeout", model_result_unknown=True
        ),
        ids["registered"]: RunOutcome(
            status="failed", error_code="model_operation_unavailable", model_result_unknown=True
        ),
        # The call succeeded but its result cannot be recovered without sending again.
        ids["lost"]: RunOutcome(
            status="failed", error_code="model_result_unavailable", model_result_unknown=True
        ),
        # A requested cancellation stays cancelled even when the deadline also passed.
        ids["cancelled"]: RunOutcome(status="cancelled"),
        ids["timeout"]: RunOutcome(status="failed", error_code="flow_timeout"),
        ids["pending"]: RunOutcome(status="pending"),
    }
    assert "private" not in str(outcomes)
    assert query(client, lambda s: flow.statuses_in_transaction(s, [ids["timeout"]])) == {
        ids["timeout"]: "failed"
    }
    with pytest.raises(ValueError):
        query(client, lambda s: flow.outcomes_in_transaction(s, [uuid4() for _ in range(101)]))


def test_file_delete_acceptance_rolls_back_with_business_and_references(notes):
    service = business(notes)
    stored = notes.portal.call(service.save, "one", b"data")
    identifier = stored["id"]
    rows = query(notes, lambda s: service.files.metadata_many_in_transaction(s, [identifier]))
    assert [row["id"] for row in rows] == [identifier]
    assert not {"path", "object_key", "storage_fingerprint"} & rows[0].keys()

    async def remove(*, rollback):
        async with service.identity.access(WRITE) as (session, auth):
            await service._record(session, auth, "one")
            await FileReferences(session, auth, "test_notes").release("one", "attachment")
            await service.files.request_delete_in_transaction(session, auth, identifier)
            await session.execute(text("DELETE FROM keystone.test_attachments WHERE key='one'"))
            if rollback:
                raise RuntimeError("business rejected")

    with pytest.raises(RuntimeError, match="business rejected"):
        notes.portal.call(lambda: remove(rollback=True))
    assert (
        query(notes, lambda s: s.scalar(select(File.status).where(File.id == identifier)))
        == "active"
    )
    assert (
        query(
            notes,
            lambda s: s.scalar(
                select(FileReference.file_id).where(FileReference.file_id == identifier)
            ),
        )
        == identifier
    )
    assert (
        query(notes, lambda s: s.scalar(text("SELECT key FROM keystone.test_attachments"))) == "one"
    )
    notes.portal.call(lambda: remove(rollback=False))
    assert (
        query(notes, lambda s: s.scalar(select(File.status).where(File.id == identifier)))
        == "deleting"
    )
    assert (
        query(notes, lambda s: s.scalar(text("SELECT key FROM keystone.test_attachments"))) is None
    )
