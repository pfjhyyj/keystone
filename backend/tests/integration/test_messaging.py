from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_identity import create_user, query

from keystone.core.credentials import KEY_FILE
from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.messaging.adapters import feishu_app, feishu_webhook
from keystone.platform.messaging.models import MessageDelivery
from keystone.platform.messaging.services import accounts as accounts_module
from keystone.platform.messaging.services import channels as channels_module
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
    hints = [row["error_hint"] for row in client.get(ROOT + "/deliveries").json()]
    assert "签名" in hints[0] and "可能已送达" in hints[1]

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


class AppSender:
    def __init__(self):
        self.sent, self.outcomes = [], []

    async def __call__(self, app_id, secret, receive_id_type, receive_id, content, uuid):
        self.sent.append((app_id, secret, receive_id_type, receive_id, uuid))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or f"om_{len(self.sent)}"


def configure_app(client):
    channel = client.post(
        ROOT + "/channels",
        json={
            "name": "飞书应用",
            "provider": "feishu_app",
            "app_id": "cli_a1b2c3",
            "credential": {"app_secret": "app-secret-value"},
        },
    )
    assert channel.status_code == 201, channel.text
    return channel.json()


def test_app_channel_targets_groups_and_lists_them_live(client, monkeypatch):
    channel = configure_app(client)
    assert channel["app_id"] == "cli_a1b2c3" and channel["destination"] is None
    assert "app-secret-value" not in client.get("/api/v1/audit-logs").text
    # The credential type is fixed by the provider.
    mismatch = client.patch(
        f"{ROOT}/channels/{channel['id']}",
        json={"name": "飞书应用", "is_active": True, "credential": {"url": HOOK}},
    )
    assert mismatch.json()["code"] == "message_credential_invalid"

    seen = []

    async def chats(app_id, secret, page_token=None):
        seen.append((app_id, secret, page_token))
        return {
            "items": [{"chat_id": "oc_ops", "name": "运维群", "external": False}],
            "page_token": None,
        }

    monkeypatch.setattr(channels_module.feishu_app, "chats", chats)
    listed = client.get(f"{ROOT}/channels/{channel['id']}/chats", params={"page_token": "p2"})
    assert listed.json()["items"][0]["chat_id"] == "oc_ops"
    assert seen == [("cli_a1b2c3", "app-secret-value", "p2")]

    # A missing scope says which permission to grant instead of a bare code.
    async def forbidden(app_id, secret, page_token=None):
        raise feishu_app.Rejected("feishu_99991672")

    monkeypatch.setattr(channels_module.feishu_app, "chats", forbidden)
    refused = client.get(f"{ROOT}/channels/{channel['id']}/chats")
    assert refused.status_code == 502 and "im:chat:readonly" in refused.json()["detail"]

    missing = client.post(
        ROOT + "/destinations",
        json={"key": "ops.app", "channel_id": channel["id"], "name": "运维群"},
    )
    assert missing.json()["code"] == "message_destination_invalid"
    for key, chat in (("ops.app", "oc_ops"), ("ops.daily", "oc_daily")):
        created = client.post(
            ROOT + "/destinations",
            json={"key": key, "channel_id": channel["id"], "name": key, "external_id": chat},
        )
        assert created.status_code == 201, created.text
    configure(client)
    app_only = client.get(ROOT + "/destinations", params={"provider": "feishu_app"}).json()
    assert [row["key"] for row in app_only] == ["ops.app", "ops.daily"]


def test_app_retries_unknown_with_the_same_uuid_inside_the_window(client, monkeypatch):
    channel = configure_app(client)
    destination = client.post(
        ROOT + "/destinations",
        json={
            "key": "ops.alerts",
            "channel_id": channel["id"],
            "name": "运维群",
            "external_id": "oc_ops",
        },
    ).json()
    sender = AppSender()
    monkeypatch.setattr(dispatch_module.feishu_app, "send", sender)

    async def age(**values):
        async with client.app.state.runtime.database.transaction() as session:
            await session.execute(update(MessageDelivery).values(**values))

    def backdate():
        client.portal.call(partial(age, updated_at=utcnow() - timedelta(minutes=1)))

    # Unanswered sends are retried with the delivery ID as uuid, at most three attempts.
    sender.outcomes = [feishu_app.Unknown(), feishu_app.Unknown(), feishu_app.Unknown()]
    delivery = add(client)
    run_dispatch(client)
    assert run_dispatch(client) == 0  # waits for the retry delay
    backdate()
    run_dispatch(client)
    backdate()
    run_dispatch(client)
    backdate()
    assert run_dispatch(client) == 0
    assert statuses(client) == [("unknown", "send_unconfirmed", 3)]
    assert {item[4] for item in sender.sent} == {str(delivery)}
    assert sender.sent[0][:4] == ("cli_a1b2c3", "app-secret-value", "chat_id", "oc_ops")

    # Outside the de-duplication window an unknown delivery is left alone.
    client.portal.call(partial(age, attempts=1, first_attempted_at=utcnow() - timedelta(hours=1)))
    backdate()
    assert run_dispatch(client) == 0

    # A refusal before sending returns a first attempt to pending; success keeps the receipt.
    sender.outcomes = [feishu_app.NotSent("provider_rate_limited")]
    add(client)
    run_dispatch(client)
    assert statuses(client)[-1] == ("pending", "provider_rate_limited", 1)
    run_dispatch(client)
    rows = query(
        client,
        lambda session: session.scalars(
            select(MessageDelivery).order_by(MessageDelivery.created_at)
        ),
    ).all()
    assert rows[-1].status == "succeeded" and rows[-1].provider_message_id.startswith("om_")
    assert rows[-1].error_code is None

    # A single app destination can be deleted with its history; group bot keys cannot.
    assert client.delete(f"{ROOT}/destinations/{destination['id']}").status_code == 204
    assert statuses(client) == []
    _, webhook_destination = configure(client, key="ops.hook")
    fixed = client.delete(f"{ROOT}/destinations/{webhook_destination['id']}")
    assert fixed.json()["code"] == "message_destination_fixed"


def test_users_are_reached_through_their_binding_and_never_redirected(client, monkeypatch):
    channel = configure_app(client)
    alice = create_user(client, "alice")
    looked_up = []

    async def open_id(app_id, secret, *, email=None, mobile=None):
        looked_up.append((app_id, secret, email, mobile))
        return {"alice@example.com": "ou_alice"}.get(email)

    monkeypatch.setattr(accounts_module.feishu_app, "open_id", open_id)
    names = ["爱丽丝"]

    async def user_name(app_id, secret, open_id):
        if not names:
            raise feishu_app.Rejected("feishu_99991672")
        return names.pop()

    monkeypatch.setattr(accounts_module.feishu_app, "user_name", user_name)
    preview = {"channel_id": channel["id"], "email": "alice@example.com"}
    found = client.post(ROOT + "/accounts/lookup", json=preview).json()
    assert found == {
        "external_user_id": "ou_alice",
        "name": "爱丽丝",
        "name_hint": None,
        "bound_to": None,
    }
    bind = {"channel_id": channel["id"], "user_id": alice["id"], "email": "alice@example.com"}
    both = client.post(ROOT + "/accounts", json={**bind, "mobile": "13800000000"})
    assert both.status_code == 422
    unknown = client.post(ROOT + "/accounts", json={**bind, "email": "nobody@example.com"})
    assert unknown.json()["code"] == "message_account_not_found"
    created = client.post(ROOT + "/accounts", json=bind)
    assert created.status_code == 201, created.text
    account = created.json()
    assert account["external_user_id"] == "ou_alice" and account["user"]["label"] == "alice"
    assert client.post(ROOT + "/accounts", json=bind).json()["code"] == "message_account_exists"
    # Without the basic info scope the preview still identifies the account and its binding.
    found = client.post(ROOT + "/accounts/lookup", json=preview).json()
    assert found["name"] is None and "contact:user.base:readonly" in found["name_hint"]
    assert found["bound_to"]["label"] == "alice"
    # The email used for the lookup is neither stored nor audited.
    assert "alice@example.com" not in client.get("/api/v1/audit-logs").text
    webhook, _ = configure(client)
    refused = client.post(ROOT + "/accounts", json={**bind, "channel_id": webhook["id"]})
    assert refused.json()["code"] == "message_channel_unsupported"

    sender = AppSender()
    monkeypatch.setattr(dispatch_module.feishu_app, "send", sender)

    async def to_user(user_id):
        async with client.app.state.runtime.database.transaction() as session:
            return await enqueue_in_transaction(
                session,
                SimpleNamespace(user=SimpleNamespace(id=None)),
                user_id=user_id,
                content={"kind": "text", "text": "审批已通过"},
                source_module="reports",
                source_type="approval",
                source_id="1",
            )

    client.portal.call(partial(to_user, UUID(alice["id"])))
    assert client.post(f"{ROOT}/accounts/{account['id']}/test").status_code == 202
    run_dispatch(client)
    assert [item[2:4] for item in sender.sent] == [("open_id", "ou_alice")] * 2
    [latest, first] = client.get(ROOT + "/deliveries").json()[:2]
    assert first["recipient"] == {"id": alice["id"], "label": "alice"}
    assert first["destination_key"] is None and latest["status"] == "succeeded"

    # Registered before unbinding or disabling: the delivery fails instead of being sent.
    client.portal.call(partial(to_user, UUID(alice["id"])))
    assert client.delete(f"{ROOT}/accounts/{account['id']}").status_code == 204
    run_dispatch(client)
    assert statuses(client)[-1] == ("failed", "recipient_unbound", 0)
    with pytest.raises(ServiceError) as error:
        client.portal.call(partial(to_user, UUID(alice["id"])))
    assert error.value.code == "message_recipient_unbound"

    account = client.post(ROOT + "/accounts", json=bind).json()
    client.portal.call(partial(to_user, UUID(alice["id"])))
    client.put(f"/api/v1/users/{alice['id']}/active", json={"is_active": False})
    run_dispatch(client)
    assert statuses(client)[-1] == ("failed", "recipient_disabled", 0)
    assert len(sender.sent) == 2

    # Deleting the channel removes its bindings with it.
    assert client.delete(f"{ROOT}/channels/{channel['id']}").status_code == 204
    assert client.get(ROOT + "/accounts").json() == []
