from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import configuration as configuration
from test_flow import start
from test_identity import assign, create_user, login, query, role
from test_model_access import SECRET, channel, entry

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


CURRENT = f"{ROLE}/current"


def test_current_role_model_is_unset_then_public_without_output_options_for_text(client):
    unset = client.get(CURRENT)
    assert unset.status_code == 200, unset.text
    assert unset.json() == {
        "definition_key": "document.review",
        "definition_revision": "1",
        "role": "review",
        "operation": "text.generate",
        "delivery": "sync",
        "status": "unset",
        "error_code": "flow_model_unset",
        "model": None,
        "output_options": None,
    }
    assert client.put(ROLE, json={"model_key": "chat"}).status_code == 200
    current = client.get(CURRENT, params={"definition_revision": "1"})
    body = current.json()
    assert body["status"] == "available" and body["error_code"] is None
    assert body["model"] == {"key": "chat", "name": "chat"}
    # A text protocol declares no image output options; nothing is guessed.
    assert body["output_options"] is None
    for private in ("openrouter", "mock-chat", SECRET, "api_base", "channel", "pricing"):
        assert private not in current.text

    stale = client.get(CURRENT, params={"definition_revision": "0"})
    assert stale.status_code == 409 and stale.json()["code"] == "flow_revision_conflict"
    assert client.get(f"{URL}/document.review/other/current").status_code == 404
    assert client.get(f"{URL}/unknown/review/current").status_code == 404


def test_current_image_role_options_come_from_the_bound_protocol(client, monkeypatch):
    from support.flow_packages import install_package
    from support.operation_flow import PACKAGE

    from keystone.platform.model_access.channels.openrouter.images import (
        OUTPUT_OPTIONS as ROUTER_OPTIONS,
    )
    from keystone.platform.model_access.channels.shared.openai_images import OUTPUT_OPTIONS
    from keystone.platform.model_access.schemas.images import MAX_RELAY_INPUT_BYTES

    install_package(client, monkeypatch, PACKAGE, engine=False)
    relay = channel(client, "yunwu", api_base="https://yunwu.ai/v1")
    gpt = entry(
        client,
        relay,
        key="gpt-image",
        name="GPT Image",
        provider_model="gpt-image-2",
        operations=["image.generate", "image.edit"],
    )
    channel_id = client.get("/api/v1/model-entries").json()[0]["channel_id"]
    entry(client, {"id": channel_id}, key="router-image", operations=["image.generate"])
    edit_role = f"{URL}/test.image_edit/edit"
    generate_role = f"{URL}/test.image/generate"
    assert client.put(edit_role, json={"model_key": "gpt-image"}).status_code == 200
    assert client.put(generate_role, json={"model_key": "router-image"}).status_code == 200

    edit = client.get(f"{edit_role}/current").json()
    assert edit["status"] == "available"
    assert edit["model"] == {"key": "gpt-image", "name": "GPT Image"}
    assert edit["output_options"] == {
        **OUTPUT_OPTIONS.view(),
        "max_reference_images": 9,
        "max_input_bytes": MAX_RELAY_INPUT_BYTES,
    }
    assert edit["output_options"]["parameters"]["aspect_ratio"] == {
        "choices": ["1:1", "16:9", "9:16", "4:3", "3:4"],
        "default": "1:1",
    }
    generate = client.get(f"{generate_role}/current").json()
    assert generate["output_options"] == {
        **ROUTER_OPTIONS.view(),
        "max_reference_images": None,
        "max_input_bytes": None,
    }
    assert set(generate["output_options"]["parameters"]) == {"resolution", "output_format"}

    # The saved default no longer serves the role: report it, never guess options.
    client.patch(
        f"/api/v1/model-entries/{gpt['id']}",
        json={
            "channel_id": relay["id"],
            "provider_model": "gpt-image-2",
            "operations": ["image.generate"],
        },
    )
    changed = client.get(f"{edit_role}/current").json()
    assert changed["status"] == "unavailable"
    assert changed["error_code"] == "model_provider_unavailable"
    assert changed["model"] == {"key": "gpt-image", "name": "GPT Image"}
    assert changed["output_options"] is None


def test_current_role_model_needs_the_right_to_start_the_flow(client):
    assert client.put(ROLE, json={"model_key": "chat"}).status_code == 200

    def user_with(name, permissions):
        user = create_user(client, name)
        query(
            client,
            lambda s: s.execute(
                update(User)
                .where(User.id == UUID(user["id"]))
                .values(password_change_required=False)
            ),
        )
        assign(client, user["id"], role(client, f"{name}-role", permissions)["id"])
        return user

    user_with("configurer", ["flow:configure", "model:invoke"])
    starter = user_with("starter", ["flow:start"])
    # Configuring defaults is not the right to start the Flow.
    login(client, "configurer")
    assert client.get(CURRENT).status_code == 403
    # Starting also needs the definition's permissions.
    login(client, "starter")
    assert client.get(CURRENT).status_code == 403
    login(client)
    required = client.app.state.runtime.flow_definitions["document.review"].required_permissions
    assign(client, starter["id"], role(client, "definition", list(required))["id"])
    login(client, "starter")
    allowed = client.get(CURRENT)
    assert allowed.status_code == 200 and allowed.json()["model"]["key"] == "chat"
