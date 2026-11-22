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
        },
    )
    assert channel.status_code == 201, channel.text
    destination = client.post(
        ROOT + "/destinations",
        json={"key": key, "channel_id": channel.json()["id"], "name": "运维群"},
    )
    assert destination.status_code == 201, destination.text
    return channel.json(), destination.json()


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
    audits = client.get("/api/v1/audit-logs").text
    assert "secret-token-value" not in audits and "sign-secret" not in audits

    invalid = client.post(
        ROOT + "/channels",
        json={
            "name": "x",
            "provider": "feishu_webhook",
            "credential": {"url": "https://evil.test/open-apis/bot/v2/hook/x"},
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

    # Renaming keeps the sealed credential; only an explicit credential replaces it.
    renamed = client.patch(
        f"{ROOT}/channels/{channel['id']}", json={"name": "运维告警", "is_active": True}
    )
    assert renamed.json()["has_credential"]
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
        json={"name": "新群", "provider": "feishu_webhook", "credential": {"url": HOOK}},
    )
    assert response.status_code == 503 and not key_file.exists()
