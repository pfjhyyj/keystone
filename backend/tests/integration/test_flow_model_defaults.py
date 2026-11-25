from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import configuration as configuration
from test_flow import start
from test_identity import assign, create_user, login, query, role
from test_model_access import entry

from keystone.platform.audit.models import AuditLog
from keystone.platform.identity.models import User

pytestmark = pytest.mark.postgres

URL = "/api/v1/flow-model-defaults"
ROLE = f"{URL}/document.review/review"


def unbound(client, file_id, key, **bindings):
    return start(client, file_id, key, model_bindings=bindings)


def saved(client):
    return [
        row.details
        for row in query(
            client,
            lambda session: session.scalars(
                select(AuditLog)
                .where(AuditLog.action == "flow.model_default.save", AuditLog.outcome == "success")
                .order_by(AuditLog.occurred_at)
            ),
        ).all()
    ]


def test_unbound_roles_take_the_default_and_explicit_bindings_win(client):
    file_id = upload(client).json()["id"]
    refused = unbound(client, file_id, "first")
    assert refused.status_code == 409 and refused.json()["code"] == "flow_model_unset"

    listing = client.get(URL).json()
    assert [(row["definition_key"], row["role"], row["model_key"]) for row in listing] == [
        ("document.review", "review", None)
    ]
    assert {"key": "chat", "name": "chat"} in client.get(f"{ROLE}/models").json()
    assert client.put(ROLE, json={"model_key": "chat"}).status_code == 200
    assert client.get(URL).json()[0]["model_key"] == "chat"

    run = unbound(client, file_id, "first")
    assert run.status_code == 202, run.text
    assert run.json()["model_bindings"] == {"review": "chat"}

    channel_id = client.get("/api/v1/model-entries").json()[0]["channel_id"]
    entry(client, {"id": channel_id}, key="other")
    explicit = unbound(client, file_id, "explicit", review="other")
    assert explicit.json()["model_bindings"] == {"review": "other"}

    # A changed default only reaches new requests; the original request replays its Run.
    assert client.put(ROLE, json={"model_key": "other"}).status_code == 200
    replay = unbound(client, file_id, "first")
    assert replay.json()["id"] == run.json()["id"]
    assert replay.json()["model_bindings"] == {"review": "chat"}
    assert unbound(client, file_id, "later").json()["model_bindings"] == {"review": "other"}
    assert saved(client) == [
        {"definition_key": "document.review", "role": "review", "before": None, "after": "chat"},
        {"definition_key": "document.review", "role": "review", "before": "chat", "after": "other"},
    ]


def test_defaults_are_checked_like_a_start(client):
    file_id = upload(client).json()["id"]
    missing = client.put(ROLE, json={"model_key": "absent"})
    assert missing.status_code == 409 and missing.json()["code"] == "model_disabled"
    assert client.put(f"{URL}/document.review/other", json={"model_key": "chat"}).status_code == 404
    assert client.get(f"{URL}/unknown/review/models").status_code == 404

    channel_id = client.get("/api/v1/model-entries").json()[0]["channel_id"]
    spare = entry(client, {"id": channel_id}, key="spare")
    assert client.put(ROLE, json={"model_key": "spare"}).status_code == 200
    client.patch(
        f"/api/v1/model-entries/{spare['id']}",
        json={"channel_id": channel_id, "provider_model": "mock-chat", "is_active": False},
    )
    # The stale default stays visible and blocks starts until it is replaced.
    assert client.get(URL).json()[0]["model_key"] == "spare"
    assert {"key": "spare", "name": "spare"} not in client.get(f"{ROLE}/models").json()
    disabled = unbound(client, file_id, "disabled")
    assert disabled.status_code == 409 and disabled.json()["code"] == "model_disabled"

    # An entry that was never used can be deleted; its default goes with it.
    assert client.delete(f"/api/v1/model-entries/{spare['id']}").status_code == 204
    assert client.get(URL).json()[0]["model_key"] is None
    assert unbound(client, file_id, "deleted").json()["code"] == "flow_model_unset"


def test_configuring_defaults_needs_its_own_permission(client):
    other = create_user(client, "configurer")
    query(
        client,
        lambda s: s.execute(
            update(User).where(User.id == UUID(other["id"])).values(password_change_required=False)
        ),
    )
    configurer = role(client, "flow-configurer", ["flow:configure"])
    assign(client, other["id"], configurer["id"])
    login(client, "configurer")
    assert client.get(URL).status_code == 200
    # Choosing a model also needs the right to use models.
    assert client.get(f"{ROLE}/models").status_code == 403
    assert client.put(ROLE, json={"model_key": "chat"}).status_code == 403

    login(client)
    invoker = role(client, "model-user", ["model:invoke"])
    assign(client, other["id"], invoker["id"])
    login(client, "configurer")
    assert client.put(ROLE, json={"model_key": "chat"}).status_code == 200

    login(client)
    plain = create_user(client, "plain")
    query(
        client,
        lambda s: s.execute(
            update(User).where(User.id == UUID(plain["id"])).values(password_change_required=False)
        ),
    )
    login(client, "plain")
    assert client.get(URL).status_code == 403
