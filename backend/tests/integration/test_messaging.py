import asyncio
import json
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_identity import create_user, query

from keystone.core.credentials import KEY_FILE
from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.messaging.adapters import feishu_app, feishu_webhook
from keystone.platform.messaging.models import (
    ExternalAccount,
    MessageConnection,
    MessageDelivery,
    MessageInbound,
)
from keystone.platform.messaging.services import accounts as accounts_module
from keystone.platform.messaging.services import channels as channels_module
from keystone.platform.messaging.services import dispatch as dispatch_module
from keystone.platform.messaging.services import inbound as inbound_module
from keystone.platform.messaging.services.actions import MessageAction
from keystone.platform.messaging.services.deliveries import enqueue_in_transaction
from keystone.platform.messaging.services.dispatch import dispatch
from keystone.platform.messaging.services.listener import Listeners
from keystone.platform.messaging.services.retention import prune_messaging

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
    return client.portal.call(dispatch, runtime, runtime.messaging_vault())


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
    failed = client.get(ROOT + "/deliveries?status=failed").json()
    assert [row["error_code"] for row in failed] == ["feishu_19021"]
    other = client.get(f"{ROOT}/deliveries?channel_id={UUID(int=1)}").json()
    assert other == [] and client.get(ROOT + "/deliveries?status=bogus").status_code == 422

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


def test_history_cleanup_removes_only_finished_deliveries_past_retention(client, sender):
    configure(client)
    old = utcnow() - timedelta(days=31)
    finished = [add(client) for _ in range(3)]
    run_dispatch(client)
    waiting, recent = add(client), add(client)
    run_dispatch(client)
    claimed = add(client)

    # Ages are set directly: one old row per open status stays, as does a recently ended one.
    async def age():
        async with client.app.state.runtime.database.transaction() as session:
            for identifier, status in (
                (finished[0], "succeeded"),
                (finished[1], "failed"),
                (finished[2], "unknown"),
                (waiting, "pending"),
                (claimed, "sending"),
            ):
                await session.execute(
                    update(MessageDelivery)
                    .where(MessageDelivery.id == identifier)
                    .values(status=status, created_at=old, updated_at=old)
                )
            await session.execute(
                update(MessageDelivery)
                .where(MessageDelivery.id == recent)
                .values(created_at=old, updated_at=utcnow() - timedelta(days=1))
            )

    client.portal.call(age)
    prune = partial(prune_messaging, client.app.state.runtime.database, 30)
    assert client.portal.call(prune)["message_deliveries"] == 3
    assert len(statuses(client)) == 6
    assert client.portal.call(partial(prune, apply=True))["message_deliveries"] == 3
    remaining = query(client, lambda session: session.scalars(select(MessageDelivery.id)))
    assert set(remaining.all()) == {waiting, recent, claimed}
    assert client.portal.call(partial(prune, apply=True))["message_deliveries"] == 0


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


def card_click(event_id, delivery_id, *, open_id="ou_admin", message_id="om_1", action="ack"):
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": event_id,
                "event_type": "card.action.trigger",
                "create_time": "1700000000000",
                "app_id": "cli_a1b2c3",
            },
            "event": {
                "operator": {"open_id": open_id},
                "action": {"value": {"delivery_id": str(delivery_id), "action": action}},
                "context": {"open_message_id": message_id, "open_chat_id": "oc_ops"},
            },
        }
    ).encode()


def private_text(message_id, text, *, open_id="ou_admin", chat_type="p2p"):
    return json.dumps(
        {
            "schema": "2.0",
            "header": {"event_id": f"e-{message_id}", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
                "message": {
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "message_type": "text",
                    "content": json.dumps({"text": text}),
                },
            },
        }
    ).encode()


def receive(client, channel, payload, slots=None):
    replies = []
    answer = client.portal.call(
        inbound_module.receive,
        client.app.state.runtime,
        {"id": UUID(channel["id"]), "app_id": "cli_a1b2c3"},
        payload,
        lambda content, open_id, key: replies.append((content["text"], open_id, key)),
        slots,
    )
    return answer, replies


def inbounds(client):
    return [
        (row["kind"], row["status"], row["error_code"])
        for row in reversed(client.get(ROOT + "/inbounds").json())
    ]


def add_to(client, key, content):
    async def register():
        async with client.app.state.runtime.database.transaction() as session:
            return await enqueue_in_transaction(
                session,
                SimpleNamespace(user=SimpleNamespace(id=None)),
                destination_key=key,
                content=content,
                source_module="reports",
                source_type="daily",
                source_id="2026-11-22",
            )

    return str(client.portal.call(register))


def test_self_binding_codes_bind_the_sender_once_and_are_never_stored(client):
    channel = configure_app(client)
    issued = client.post(ROOT + "/bind-codes")
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]
    assert len(code) == 8
    [mine] = client.get(ROOT + "/me/accounts").json()
    assert mine["channel_name"] == "飞书应用" and mine["account"] is None

    # Group messages and other text are ignored without a record.
    group = private_text("om_g", f"绑定 {code}", chat_type="group")
    assert receive(client, channel, group) == (None, [])
    assert receive(client, channel, private_text("om_x", "你好"))[1] == []
    _, replies = receive(client, channel, private_text("om_1", f"绑定 {code.lower()}"))
    [account] = client.get(ROOT + "/accounts").json()
    assert account["external_user_id"] == "ou_admin"
    label = account["user"]["label"]
    assert replies == [(f"已绑定到 Keystone 用户 {label}。", "ou_admin", "om_1")]
    # Feishu may push the same message twice; it is answered once.
    assert receive(client, channel, private_text("om_1", f"绑定 {code}"))[1] == []
    # The code is used up.
    _, replies = receive(client, channel, private_text("om_2", f"绑定 {code}"))
    assert "无效" in replies[0][0]

    # A newer code moves the user to the account that sent it.
    newer = client.post(ROOT + "/bind-codes").json()["code"]
    _, replies = receive(client, channel, private_text("om_3", f"绑定 {newer}", open_id="ou_new"))
    assert "已绑定到" in replies[0][0]
    [account] = client.get(ROOT + "/accounts").json()
    assert account["external_user_id"] == "ou_new"

    # An account already bound to someone else is not taken over.
    alice = create_user(client, "alice")
    taken = {"channel_id": channel["id"], "user_id": alice["id"]}

    async def bind_alice():
        async with client.app.state.runtime.database.transaction() as session:
            session.add(
                ExternalAccount(
                    channel_id=UUID(taken["channel_id"]),
                    user_id=UUID(taken["user_id"]),
                    external_user_id="ou_alice",
                )
            )

    client.portal.call(bind_alice)
    other = client.post(ROOT + "/bind-codes").json()["code"]
    _, replies = receive(client, channel, private_text("om_4", f"绑定 {other}", open_id="ou_alice"))
    assert "已绑定其他" in replies[0][0]

    # Failures count per Feishu account: one earlier wrong code plus four more.
    for index in range(4):
        receive(client, channel, private_text(f"om_bad{index}", "绑定 ZZZZZZZZ"))
    _, replies = receive(client, channel, private_text("om_bad9", "绑定 ZZZZZZZZ"))
    assert "次数过多" in replies[0][0]
    assert inbounds(client)[0] == ("bind", "processed", None)
    assert inbounds(client)[-1] == ("bind", "rejected", "bind_rate_limited")
    audits = client.get("/api/v1/audit-logs?action=messaging.account.self_bind").json()
    assert [item["details"]["replaced"] for item in audits] == [True, False]

    # Users see and remove only their own binding.
    [mine] = client.get(ROOT + "/me/accounts").json()
    assert mine["account"]["external_user_id"] == "ou_new"
    [alice_account] = [
        row for row in client.get(ROOT + "/accounts").json() if row["user"]["id"] == alice["id"]
    ]
    foreign = client.delete(f"{ROOT}/me/accounts/{alice_account['id']}")
    assert foreign.json()["code"] == "message_account_not_found"
    assert client.delete(f"{ROOT}/me/accounts/{mine['account']['id']}").status_code == 204
    assert client.get(ROOT + "/me/accounts").json()[0]["account"] is None
    removed = client.get("/api/v1/audit-logs?action=messaging.account.self_unbind").json()
    assert removed[0]["details"]["external_user_id"] == "ou_new"
    text = client.get("/api/v1/audit-logs").text + client.get(ROOT + "/inbounds").text
    assert code not in text and newer not in text and other not in text


def test_card_actions_run_as_the_bound_user_and_answer_inside_the_deadline(client, monkeypatch):
    channel = configure_app(client)
    destination = client.post(
        ROOT + "/destinations",
        json={
            "key": "ops.app",
            "channel_id": channel["id"],
            "name": "运维群",
            "external_id": "oc_ops",
        },
    )
    assert destination.status_code == 201, destination.text
    _, hook_destination = configure(client)
    refused = client.post(f"{ROOT}/destinations/{hook_destination['id']}/test?card=true")
    assert refused.json()["code"] == "message_actions_unsupported"
    sender = AppSender()
    monkeypatch.setattr(dispatch_module.feishu_app, "send", sender)
    sent = client.post(f"{ROOT}/destinations/{destination.json()['id']}/test?card=true")
    delivery_id = sent.json()["delivery_id"]
    run_dispatch(client)

    # Not bound yet: refused with a hint, and audited as denied.
    answer, _ = receive(client, channel, card_click("e1", delivery_id))
    assert answer["toast"]["type"] == "error" and "绑定" in answer["toast"]["content"]
    assert inbounds(client) == [("card_action", "rejected", "message_actor_unbound")]
    denied = client.get("/api/v1/audit-logs?action=messaging.inbound.card_action").json()
    assert denied[0]["outcome"] == "denied"

    code = client.post(ROOT + "/bind-codes").json()["code"]
    receive(client, channel, private_text("om_bind", f"绑定 {code}"))
    label = client.get(ROOT + "/accounts").json()[0]["user"]["label"]
    answer, _ = receive(client, channel, card_click("e2", delivery_id))
    assert answer["toast"] == {"type": "success", "content": "已确认收到"}
    card = answer["card"]["data"]
    assert card["schema"] == "2.0" and card["config"]["update_multi"] is True
    assert f"{label} 已确认收到" in json.dumps(card, ensure_ascii=False)
    assert "callback" not in json.dumps(card)
    # A repeated callback is answered from the stored outcome without running again.
    again, _ = receive(client, channel, card_click("e2", delivery_id))
    assert again == {"toast": {"type": "success", "content": "已处理"}}
    # Values copied into another message do not match the clicked card.
    forged, _ = receive(client, channel, card_click("e3", delivery_id, message_id="om_other"))
    assert forged["toast"]["type"] == "error"
    unknown, _ = receive(client, channel, card_click("e4", delivery_id, action="approve"))
    assert unknown["toast"]["type"] == "error"
    assert [row for row in inbounds(client) if row[0] == "card_action"][-3:] == [
        ("card_action", "processed", None),
        ("card_action", "rejected", "message_action_mismatch"),
        ("card_action", "rejected", "message_action_mismatch"),
    ]
    rows = client.get(ROOT + "/inbounds").json()
    [row] = [row for row in rows if row["kind"] == "card_action" and row["status"] == "processed"]
    assert row["actor"]["label"] == label

    # Business actions: a permission check, a refusal and a handler too slow for the deadline.
    calls = []

    async def approve(runtime, identity, source_id, action_key, inbound_id):
        async with identity.access("messaging:manage") as (_, auth):
            calls.append((source_id, action_key, auth.user.username))
        if len(calls) == 2:
            raise ServiceError(409, "report_closed", "报表已关闭")
        if len(calls) == 3:
            await asyncio.sleep(5)

    runtime = client.app.state.runtime
    monkeypatch.setattr(
        runtime,
        "message_actions",
        {
            **runtime.message_actions,
            ("reports", "daily", "approve"): MessageAction(
                "reports", "daily", "approve", "messaging:manage", approve
            ),
        },
    )
    monkeypatch.setattr(inbound_module, "BUDGET_SECONDS", 0.5)
    approval = add_to(
        client,
        "ops.app",
        {
            "kind": "card",
            "title": "日报",
            "actions": [{"key": "approve", "text": "批准", "style": "primary"}],
        },
    )
    run_dispatch(client)
    message_id = f"om_{len(sender.sent)}"

    def click(event_id):
        payload = card_click(event_id, approval, message_id=message_id, action="approve")
        return receive(client, channel, payload)[0]

    assert click("e5")["toast"]["content"] == "已批准"
    assert calls == [("2026-11-22", "approve", "admin")]
    assert click("e6")["toast"] == {"type": "error", "content": "报表已关闭"}
    assert click("e7") == {"toast": {"type": "info", "content": "已收到，正在处理"}}
    assert inbounds(client)[-3:] == [
        ("card_action", "processed", None),
        ("card_action", "rejected", "report_closed"),
        ("card_action", "received", None),
    ]

    # The periodic pass finishes it after the lease and updates the card in place.
    updated = []

    async def update_card(app_id, secret, message, card):
        updated.append((message, json.dumps(card, ensure_ascii=False)))

    monkeypatch.setattr(inbound_module.feishu_app, "update_card", update_card)

    async def expire_lease():
        async with runtime.database.transaction() as session:
            await session.execute(
                update(MessageInbound)
                .where(MessageInbound.status == "received")
                .values(updated_at=utcnow() - timedelta(minutes=2))
            )

    client.portal.call(expire_lease)
    vault = runtime.messaging_vault()
    assert client.portal.call(inbound_module.retry, runtime, vault) == 1
    assert inbounds(client)[-1] == ("card_action", "processed", None)
    assert updated[0][0] == message_id and f"{label} 已批准" in updated[0][1]
    assert client.portal.call(inbound_module.retry, runtime, vault) == 0

    # Deleting the channel removes its inbound history with it.
    assert client.delete(f"{ROOT}/channels/{channel['id']}").status_code == 204
    assert client.get(ROOT + "/inbounds").json() == []


def test_workers_follow_app_channel_changes_with_one_connection_each(client):
    channel = configure_app(client)
    configure(client)
    runtime = client.app.state.runtime
    endpoints = []

    def answer(request):
        endpoints.append(json.loads(request.content))
        return httpx2.Response(200, json={"code": 1000040350, "msg": "limit"})

    listeners = Listeners(
        runtime, runtime.messaging_vault(), transport=httpx2.MockTransport(answer)
    )

    async def settle():
        await listeners.sync()
        await asyncio.sleep(0.1)

    try:
        client.portal.call(settle)
        assert list(listeners.running) == [UUID(channel["id"])]
        assert endpoints == [{"AppID": "cli_a1b2c3", "AppSecret": "app-secret-value"}]
        client.portal.call(settle)
        assert len(endpoints) == 1
        # The refusal and its reason reach the console with the channel.
        app = client.get(f"{ROOT}/channels/{channel['id']}").json()
        assert app["connection"]["state"] == "refused"
        assert app["connection"]["error_code"] == "feishu_ws_1000040350"
        assert "50" in app["connection"]["error_hint"]
        assert app["groups"] == 0 and app["members"] == 0
        listed = {row["id"]: row for row in client.get(ROOT + "/channels").json()}
        assert listed[channel["id"]]["connection"]["state"] == "refused"
        hooks = [row for row in listed.values() if row["provider"] == "feishu_webhook"]
        assert "connection" not in hooks[0]

        # Rows a stopped Worker left behind stop counting once they are no longer refreshed.
        async def age():
            async with runtime.database.transaction() as session:
                await session.execute(
                    update(MessageConnection).values(updated_at=utcnow() - timedelta(minutes=5))
                )

        client.portal.call(age)
        stale = client.get(f"{ROOT}/channels/{channel['id']}").json()["connection"]
        assert stale == {"state": "offline", "workers": 0}
        # A changed secret reconnects with it; disabling closes the connection.
        client.patch(
            f"{ROOT}/channels/{channel['id']}",
            json={
                "name": "飞书应用",
                "is_active": True,
                "credential": {"app_secret": "rotated-secret"},
            },
        )
        client.portal.call(settle)
        assert endpoints[-1]["AppSecret"] == "rotated-secret" and len(listeners.running) == 1
        client.patch(
            f"{ROOT}/channels/{channel['id']}", json={"name": "飞书应用", "is_active": False}
        )
        client.portal.call(settle)
        assert listeners.running == {}
        rows = query(client, lambda session: session.scalars(select(MessageConnection)))
        assert rows.all() == []
    finally:
        client.portal.call(listeners.close)


def test_a_busy_worker_defers_clicks_to_the_periodic_pass(client, monkeypatch):
    channel = configure_app(client)
    destination = client.post(
        ROOT + "/destinations",
        json={
            "key": "ops.app",
            "channel_id": channel["id"],
            "name": "运维群",
            "external_id": "oc_ops",
        },
    ).json()
    sender = AppSender()
    monkeypatch.setattr(dispatch_module.feishu_app, "send", sender)
    code = client.post(ROOT + "/bind-codes").json()["code"]
    receive(client, channel, private_text("om_bind", f"绑定 {code}"))
    sent = client.post(f"{ROOT}/destinations/{destination['id']}/test?card=true")
    delivery_id = sent.json()["delivery_id"]
    run_dispatch(client)
    updated = []

    async def update_card(app_id, secret, message, card):
        updated.append(message)

    monkeypatch.setattr(inbound_module.feishu_app, "update_card", update_card)
    monkeypatch.setattr(inbound_module, "BUDGET_SECONDS", 0.2)

    async def busy(payload):
        # Every slot is taken: the click is kept and answered without being processed.
        slots = asyncio.Semaphore(0)
        return await inbound_module.receive(
            client.app.state.runtime,
            {"id": UUID(channel["id"]), "app_id": "cli_a1b2c3"},
            payload,
            lambda *_: None,
            slots,
        )

    click = card_click("e1", delivery_id, message_id=f"om_{len(sender.sent)}")
    assert client.portal.call(busy, click) == {
        "toast": {"type": "info", "content": "已收到，正在处理"}
    }
    assert inbounds(client)[-1] == ("card_action", "received", None)
    # A binding message has nothing to resume from, so it is left unanswered for redelivery.
    with pytest.raises(TimeoutError):
        client.portal.call(busy, private_text("om_again", "绑定 ABCDEFGH"))
    # The next pass takes the deferred click at once; no lease has to run out first.
    runtime = client.app.state.runtime
    assert client.portal.call(inbound_module.retry, runtime, runtime.messaging_vault()) == 1
    assert inbounds(client)[-1] == ("card_action", "processed", None)
    assert updated == [f"om_{len(sender.sent)}"]
