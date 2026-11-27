from uuid import UUID, uuid4

import pytest
from support.flow_fixture.contracts import definition
from support.flow_packages import snapshot_for
from test_files_dictionary import client as client
from test_identity import bearer, create_user, login, query, token
from test_messaging import ROOT, configure

from keystone.platform.flow.models import FlowRun
from keystone.platform.messaging.models import ExternalAccount, MessageDelivery, MessageInbound

pytestmark = pytest.mark.postgres
OPT_IN = {"X-Keystone-List-Total": "1"}
TOTAL = "X-Total-Count"


def counted_page(client, path, total, size, *, headers=None, **params):
    response = client.get(path, params=params, headers={**(headers or {}), **OPT_IN})
    assert response.status_code == 200, response.text
    assert response.headers[TOTAL] == str(total)
    rows = response.json()
    assert isinstance(rows, list) and len(rows) == size
    return rows


def test_flow_run_total_respects_status_and_effective_actor_scope(client):
    admin = UUID(login(client)["id"])
    other = UUID(create_user(client)["id"])
    own_only = bearer(token(client, ["flow:read"])["token"])
    snapshot = snapshot_for(definition)

    async def seed(session):
        session.add_all(
            FlowRun(
                actor_id=actor,
                status=status,
                request_key=str(uuid4()),
                request_digest="1" * 64,
                definition_key=definition.key,
                definition_snapshot=snapshot,
                execution_version="2" * 64,
                input={},
            )
            for actor, status in (
                (admin, "pending"),
                (admin, "pending"),
                (admin, "running"),
                (other, "pending"),
            )
        )

    query(client, seed)
    path = "/api/v1/flow-runs"
    counted_page(client, path, 3, 1, status="pending", limit=1)
    [row] = counted_page(client, path, 2, 1, headers=own_only, status="pending", limit=1)
    assert row["actor_id"] == str(admin) and row["status"] == "pending"
    counted_page(client, path, 2, 0, headers=own_only, status="pending", limit=1, offset=2)
    counted_page(client, path, 0, 0, headers=own_only, status="succeeded")
    ordinary = client.get(path, params={"status": "pending", "limit": 1}, headers=own_only)
    assert ordinary.status_code == 200 and ordinary.json() == [row]
    assert TOTAL not in ordinary.headers


def app_channel(client, suffix):
    response = client.post(
        ROOT + "/channels",
        json={
            "name": f"App {suffix}",
            "provider": "feishu_app",
            "app_id": f"cli_{suffix}",
            "credential": {"app_secret": "test-app-secret"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_messaging_list_totals_respect_filters_and_permissions(client):
    admin = UUID(login(client)["id"])
    other = UUID(create_user(client)["id"])
    a, b = app_channel(client, "a1b2c3"), app_channel(client, "b2c3d4")
    configure(client)  # A webhook channel also creates its own destination.
    destinations = []
    for channel_id, key, external_id in (
        (a, "app.one", "oc_1"),
        (a, "app.two", "oc_2"),
        (b, "app.three", "oc_3"),
    ):
        response = client.post(
            ROOT + "/destinations",
            json={"channel_id": channel_id, "key": key, "name": key, "external_id": external_id},
        )
        assert response.status_code == 201, response.text
        destinations.append(UUID(response.json()["id"]))

    async def seed(session):
        for channel_id, user, external_user_id in (
            (a, admin, "ou_1"),
            (a, other, "ou_2"),
            (b, admin, "ou_3"),
        ):
            session.add(
                ExternalAccount(
                    channel_id=UUID(channel_id), user_id=user, external_user_id=external_user_id
                )
            )
        for channel_id, inbound_status, delivery_status, destination in (
            (a, "processed", "pending", destinations[0]),
            (a, "processed", "pending", None),
            (a, "received", "failed", destinations[1]),
            (b, "processed", "pending", destinations[2]),
        ):
            source = str(uuid4())
            session.add_all(
                (
                    MessageInbound(
                        channel_id=UUID(channel_id),
                        dedupe_key=source,
                        kind="bind",
                        external_user_id="ou_1",
                        status=inbound_status,
                    ),
                    MessageDelivery(
                        channel_id=UUID(channel_id),
                        destination_id=destination,
                        recipient_user_id=admin if destination is None else None,
                        source_module="test",
                        source_type="test",
                        source_id=source,
                        kind="text",
                        content_mode="supplied",
                        status=delivery_status,
                    ),
                )
            )

    query(client, seed)
    manage_only = bearer(token(client, ["messaging:manage"])["token"])
    read_only = bearer(token(client, ["message_delivery:read"])["token"])
    for name, filters, total, forbidden_as in (
        ("channels", {}, 3, read_only),
        ("destinations", {"provider": "feishu_app", "channel_id": a}, 2, read_only),
        ("accounts", {"channel_id": a}, 2, read_only),
        ("inbounds", {"channel_id": a, "status": "processed"}, 2, manage_only),
        ("deliveries", {"channel_id": a, "status": "pending"}, 2, manage_only),
    ):
        path = f"{ROOT}/{name}"
        rows = counted_page(client, path, total, 1, limit=1, **filters)
        counted_page(client, path, total, 0, limit=1, offset=total, **filters)
        ordinary = client.get(path, params={**filters, "limit": 1})
        assert ordinary.status_code == 200 and ordinary.json() == rows
        assert TOTAL not in ordinary.headers
        denied = client.get(path, params=filters, headers={**forbidden_as, **OPT_IN})
        assert denied.status_code == 403 and TOTAL not in denied.headers

    counted_page(client, f"{ROOT}/destinations", 0, 0, provider="feishu_webhook", channel_id=a)
    deliveries = counted_page(client, f"{ROOT}/deliveries", 2, 2, channel_id=a, status="pending")
    assert {delivery["destination_key"] for delivery in deliveries} == {"app.one", None}
