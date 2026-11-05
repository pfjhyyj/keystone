"""Public batch reads and business-owned deletion on real PostgreSQL."""

from uuid import UUID

import pytest
from sqlalchemy import select, text
from support.flow_admission import accept
from test_files_dictionary import client as client
from test_identity import create_user, query, role
from test_managed_files import WRITE, business
from test_managed_files import notes as notes

from keystone.platform.dictionary.services.dictionaries import describe_items_in_transaction
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services.references import FileReferences
from keystone.platform.identity.services.subjects import role_in_transaction, users_in_transaction

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
