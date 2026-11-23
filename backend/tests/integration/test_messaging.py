from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_identity import query

from keystone.core.credentials import KEY_FILE
from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.messaging.adapters import feishu_webhook
from keystone.platform.messaging.models import MessageDelivery
from keystone.platform.messaging.services import dispatch as dispatch_module
from keystone.platform.messaging.services.deliveries import enqueue_in_transaction
from keystone.platform.messaging.services.dispatch import dispatch

pytestmark = pytest.mark.postgres
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token-value"
ROOT = "/api/v1/messaging"
TEST_TEXT = {"kind": "text", "text": "Keystone 测试消息：渠道配置可用。"}


def configure(client, key="ops.alerts"):
    channel = client.post(
        ROOT + "/channels",
        json={
            "name": "运维群",
            "provider": "feishu_webhook",
            "credential": {"url": HOOK, "secret": "sign-secret"},
            "destination_key": key,
        },
    )
    assert channel.status_code == 201, channel.text
    return channel.json(), channel.json()["destination"]


class Sender:
    def __init__(self):
        self.sent, self.outcome = [], None

    async def __call__(self, url, secret, content):
        self.sent.append((url, secret, content))
        if self.outcome is not None:
            raise self.outcome


@pytest.fixture
def sender(monkeypatch):
    result = Sender()
    monkeypatch.setattr(dispatch_module.feishu_webhook, "send", result)
    return result


def run_dispatch(client):
    runtime = client.app.state.runtime
    return client.portal.call(dispatch, runtime.database, runtime.messaging_vault())


def statuses(client):
    rows = query(
        client,
        lambda session: session.scalars(
            select(MessageDelivery).order_by(MessageDelivery.created_at)
        ),
    )
    return [(row.status, row.error_code, row.attempts) for row in rows.all()]


def add(client, **extra):
    # A business registers deliveries inside its own authorized transaction.
    async def register():
        async with client.app.state.runtime.database.transaction() as session:
            return await enqueue_in_transaction(
                session,
                SimpleNamespace(user=SimpleNamespace(id=None)),
                destination_key="ops.alerts",
                content={
                    "kind": "card",
                    "title": "日报",
                    "fields": [{"label": "状态", "value": "完成"}],
                },
                source_module="reports",
                source_type="daily",
                source_id="2026-11-22",
                **extra,
            )

    return client.portal.call(register)


def test_webhook_channel_is_write_only_and_test_message_is_sent_once(client, sender):
    channel, destination = configure(client)
    assert channel["has_credential"] and "credential" not in channel
    assert destination["key"] == "ops.alerts"
    [listed] = client.get(ROOT + "/channels").json()
    assert listed["destination"] == destination
    audits = client.get("/api/v1/audit-logs").text
    assert "secret-token-value" not in audits and "sign-secret" not in audits

    invalid = client.post(
        ROOT + "/channels",
        json={
            "name": "x",
            "provider": "feishu_webhook",
            "credential": {"url": "https://evil.test/open-apis/bot/v2/hook/x"},
            "destination_key": "ops.evil",
        },
    )
    assert invalid.status_code == 422
    second = client.post(
        ROOT + "/destinations",
        json={"key": "ops.other", "channel_id": channel["id"], "name": "另一个"},
    )
    assert second.json()["code"] == "message_destination_invalid"

    assert client.post(f"{ROOT}/destinations/{destination['id']}/test").status_code == 202
    assert run_dispatch(client) == 1
    assert sender.sent == [(HOOK, "sign-secret", TEST_TEXT)]
    assert run_dispatch(client) == 0
    [view] = client.get(ROOT + "/deliveries").json()
    assert view["status"] == "succeeded" and view["destination_key"] == "ops.alerts"
    assert "content" not in view

    # A taken key is refused with the whole channel; nothing is half-created.
    duplicate = client.post(
        ROOT + "/channels",
        json={
            "name": "重复",
            "provider": "feishu_webhook",
            "credential": {"url": HOOK},
            "destination_key": "ops.alerts",
        },
    )
    assert duplicate.status_code == 409
    assert len(client.get(ROOT + "/channels").json()) == 1

    # Renaming keeps the sealed credential and the group bot's destination name in step.
    renamed = client.patch(
        f"{ROOT}/channels/{channel['id']}", json={"name": "运维告警", "is_active": True}
    )
    assert renamed.json()["has_credential"]
    [listed] = client.get(ROOT + "/destinations").json()
    assert listed["name"] == "运维告警"
    client.post(f"{ROOT}/destinations/{destination['id']}/test")
    run_dispatch(client)
    assert sender.sent[-1][:2] == (HOOK, "sign-secret")


def test_outcomes_are_final_and_deadlines_and_disabled_channels_hold(client, sender):
    channel, _ = configure(client)

    # An unanswered send stays unknown and is never resent: group bots cannot de-duplicate.
    sender.outcome = feishu_webhook.Unknown()
    add(client)
    run_dispatch(client)
    sender.outcome = feishu_webhook.Rejected("feishu_19021")
    add(client)
    run_dispatch(client)
    sender.outcome = None
    assert run_dispatch(client) == 0
    assert statuses(client) == [("unknown", "send_unconfirmed", 1), ("failed", "feishu_19021", 1)]

    # A deadline that passes before sending ends the delivery without a send.
    expiring = add(client, expires_at=utcnow() + timedelta(seconds=30))

    async def expire():
        async with client.app.state.runtime.database.transaction() as session:
            await session.execute(
                update(MessageDelivery)
                .where(MessageDelivery.id == expiring)
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )

    client.portal.call(expire)
    sent = len(sender.sent)
    run_dispatch(client)
    assert statuses(client)[-1][0] == "expired" and len(sender.sent) == sent

    # Registered deliveries wait while the channel is disabled; new ones are refused.
    add(client)
    client.patch(f"{ROOT}/channels/{channel['id']}", json={"name": "运维群", "is_active": False})
    assert run_dispatch(client) == 0
    assert statuses(client)[-1][0] == "pending"
    with pytest.raises(ServiceError) as error:
        add(client)
    assert error.value.code == "message_destination_unavailable"
    client.patch(f"{ROOT}/channels/{channel['id']}", json={"name": "运维群", "is_active": True})
    assert run_dispatch(client) == 1
    assert statuses(client)[-1][0] == "succeeded"


def test_missing_key_is_not_replaced_while_ciphertexts_exist(client):
    configure(client)
    key_file = client.app.state.runtime.settings.file_coordination_root / KEY_FILE
    key_file.unlink()
    response = client.post(
        ROOT + "/channels",
        json={
            "name": "新群",
            "provider": "feishu_webhook",
            "credential": {"url": HOOK},
            "destination_key": "ops.new",
        },
    )
    assert response.status_code == 503 and not key_file.exists()


def test_deleting_a_channel_removes_its_key_and_history_unless_a_send_is_running(client, sender):
    channel, _ = configure(client)
    add(client)
    run_dispatch(client)
    pending = add(client)

    # A delivery being sent right now blocks the removal, and nothing is half-removed.
    async def mark_sending():
        async with client.app.state.runtime.database.transaction() as session:
            await session.execute(
                update(MessageDelivery)
                .where(MessageDelivery.id == pending)
                .values(status="sending", updated_at=utcnow())
            )

    client.portal.call(mark_sending)
    busy = client.delete(f"{ROOT}/channels/{channel['id']}")
    assert busy.json()["code"] == "message_channel_sending"
    assert len(statuses(client)) == 2 and len(client.get(ROOT + "/channels").json()) == 1

    async def finish():
        async with client.app.state.runtime.database.transaction() as session:
            await session.execute(
                update(MessageDelivery)
                .where(MessageDelivery.id == pending)
                .values(status="unknown", error_code="send_unconfirmed")
            )

    client.portal.call(finish)
    assert client.delete(f"{ROOT}/channels/{channel['id']}").status_code == 204
    assert client.get(ROOT + "/channels").json() == []
    assert client.get(ROOT + "/destinations").json() == []
    assert statuses(client) == []
    [audit] = client.get(
        "/api/v1/audit-logs",
        params={"action": "messaging.channel.delete", "outcome": "success"},
    ).json()
    assert audit["details"]["destination_keys"] == ["ops.alerts"]
    assert audit["details"]["deliveries_removed"] == 2
    assert client.delete(f"{ROOT}/channels/{channel['id']}").status_code == 404

    # The key is free again once its channel is gone.
    configure(client)
