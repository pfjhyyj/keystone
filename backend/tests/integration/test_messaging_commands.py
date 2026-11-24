"""Commands, executing users, replies and content supplied at send time."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_identity import assign, bearer, create_user, query, role, token
from test_messaging import (
    ROOT,
    card_click,
    configure,
    configure_app,
    inbounds,
    private_text,
    receive,
    run_dispatch,
)

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.models import User
from keystone.platform.messaging.models import MessageDelivery, MessageInbound
from keystone.platform.messaging.services import dispatch as dispatch_module
from keystone.platform.messaging.services import inbound as inbound_module
from keystone.platform.messaging.services.actions import (
    MessageAction,
    MessageCommand,
    build_actions,
    build_commands,
)
from keystone.platform.messaging.services.deliveries import enqueue_in_transaction

pytestmark = pytest.mark.postgres
PERMISSIONS = {"messaging:manage": "", "message_delivery:read": ""}


class Provider:
    """Feishu sends and replies; records what went out, never calls the network."""

    def __init__(self):
        self.sent, self.replies = [], []

    async def send(self, app_id, secret, receive_id_type, receive_id, content, uuid):
        self.sent.append((receive_id_type, receive_id, content, uuid))
        return f"om_sent{len(self.sent)}"

    async def reply(self, app_id, secret, message_id, content, uuid):
        self.replies.append((message_id, content, uuid))
        return f"om_reply{len(self.replies)}"


@pytest.fixture
def provider(monkeypatch):
    result = Provider()
    monkeypatch.setattr(dispatch_module.feishu_app, "send", result.send)
    monkeypatch.setattr(dispatch_module.feishu_app, "reply", result.reply)
    return result


def group_text(message_id, text, *, chat_id="oc_ops", open_id="ou_admin", mentioned=True):
    body = json.loads(private_text(message_id, text, open_id=open_id, chat_type="group"))
    body["event"]["message"]["chat_id"] = chat_id
    if mentioned:
        body["event"]["message"]["mentions"] = [
            {"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Keystone"}
        ]
    return json.dumps(body).encode()


def stale(payload):
    # Feishu stamps the message when it was sent; this one is years old.
    body = json.loads(payload)
    body["header"]["create_time"] = "1700000000000"
    return json.dumps(body).encode()


def add_group(client, channel, key="ops.app", chat_id="oc_ops"):
    destination = client.post(
        ROOT + "/destinations",
        json={"key": key, "channel_id": channel["id"], "name": "运维群", "external_id": chat_id},
    )
    assert destination.status_code == 201, destination.text
    return destination.json()


def bind_admin(client, channel):
    code = client.post(ROOT + "/bind-codes").json()["code"]
    receive(client, channel, private_text(f"om_bind_{code}", f"绑定 {code}"))


def declare(monkeypatch, client, *, commands=(), actions=()):
    runtime = client.app.state.runtime
    monkeypatch.setattr(runtime, "message_commands", build_commands(commands, PERMISSIONS))
    monkeypatch.setattr(runtime, "message_actions", build_actions(actions, PERMISSIONS))


def failure_replies(client):
    rows = query(
        client,
        lambda session: session.scalars(
            select(MessageDelivery).where(MessageDelivery.source_module == "messaging")
        ),
    ).all()
    return {row.reply_inbound_id: row.content["text"] for row in rows if row.reply_inbound_id}


async def reply_to(identity, inbound_id, content, module="reports", source_type="subscription"):
    async with identity.access("messaging:manage") as (session, auth):
        return await enqueue_in_transaction(
            session,
            auth,
            inbound_id=inbound_id,
            content=content,
            source_module=module,
            source_type=source_type,
            source_id="1",
        )


def test_commands_are_matched_whole_and_answered_by_replies(client, provider, monkeypatch):
    channel = configure_app(client)
    add_group(client, channel)
    calls = []

    async def subscribe(identity, inbound_id, command_key):
        async with identity.access("messaging:manage") as (_, auth):
            calls.append((auth.user.username, command_key))
        if len(calls) == 3:
            raise ServiceError(409, "subscription_exists", "已经订阅过了")
        await reply_to(identity, inbound_id, {"kind": "text", "text": "已收到，正在准备订阅链接"})

    async def quiet(identity, inbound_id, command_key):
        calls.append(("quiet", command_key))

    minute = timedelta(minutes=1)
    declare(
        monkeypatch,
        client,
        commands=(
            MessageCommand(
                "reports",
                "subscribe",
                "订阅",
                ("p2p", "group"),
                "messaging:manage",
                subscribe,
                minute,
                reply_failures=True,
            ),
            MessageCommand("reports", "quiet", "安静", ("p2p",), None, quiet, minute),
        ),
    )

    # Not bound: refused, audited, and answered only where the command asks for it.
    receive(client, channel, private_text("om_c1", "订阅"))
    receive(client, channel, private_text("om_c2", " 安静 "))
    assert inbounds(client) == [
        ("command", "rejected", "message_actor_unbound"),
        ("command", "rejected", "message_actor_unbound"),
    ]
    denied = client.get("/api/v1/audit-logs?action=messaging.inbound.command").json()
    assert [item["outcome"] for item in denied] == ["denied", "denied"]
    [(first, text)] = failure_replies(client).items()
    assert "绑定" in text
    run_dispatch(client)
    assert provider.replies == [("om_c1", {"kind": "text", "text": text}, str_id(client, first))]

    # Bound now: the handler accepts and replies to the very message.
    bind_admin(client, channel)
    receive(client, channel, private_text("om_c3", "订阅"))
    assert calls == [("admin", "subscribe")]
    # Feishu may push a message twice; it is processed once.
    receive(client, channel, private_text("om_c3", "订阅"))
    assert calls == [("admin", "subscribe")]
    run_dispatch(client)
    assert provider.replies[-1][:2] == (
        "om_c3",
        {"kind": "text", "text": "已收到，正在准备订阅链接"},
    )

    # Groups: only mentioned messages in configured groups, matched without the mention.
    before = len(inbounds(client))
    receive(client, channel, group_text("om_g1", "@_user_1 订阅", chat_id="oc_other"))
    receive(client, channel, group_text("om_g2", "订阅", mentioned=False))
    receive(client, channel, group_text("om_g3", "@_user_1 订阅一下"))
    receive(client, channel, group_text("om_g4", "@_user_1 安静"))
    assert len(inbounds(client)) == before
    receive(client, channel, group_text("om_g5", "@_user_1 订阅"))
    assert calls[-1] == ("admin", "subscribe")
    [row] = [
        row
        for row in client.get(ROOT + "/inbounds").json()
        if row["id"] == inbound_of(client, "om_g5")
    ]
    assert row["destination_id"] and row["command_key"] == "subscribe"

    # A business refusal is answered with its message; a stale command expires.
    receive(client, channel, private_text("om_c4", "订阅"))
    receive(client, channel, stale(private_text("om_c5", "订阅")))
    assert inbounds(client)[-2:] == [
        ("command", "rejected", "subscription_exists"),
        ("command", "expired", None),
    ]
    replies = failure_replies(client)
    assert replies[UUID(inbound_of(client, "om_c4"))] == "已经订阅过了"
    assert replies[UUID(inbound_of(client, "om_c5"))] == "命令已过期，请重新发送"

    # A command that keeps failing ends as failed and is answered once.
    async def deferred():
        return await inbound_module.receive(
            client.app.state.runtime,
            {"id": UUID(channel["id"]), "app_id": "cli_a1b2c3"},
            private_text("om_c6", "订阅"),
            lambda *_: None,
            asyncio.Semaphore(0),
        )

    client.portal.call(deferred)
    unfinished = UUID(inbound_of(client, "om_c6"))
    runtime = client.app.state.runtime

    async def exhaust():
        async with runtime.database.transaction() as session:
            await session.execute(
                update(MessageInbound)
                .where(MessageInbound.id == unfinished)
                .values(attempts=3, updated_at=utcnow() - timedelta(minutes=2))
            )

    client.portal.call(exhaust)
    client.portal.call(inbound_module.retry, runtime, runtime.messaging_vault())
    assert inbounds(client)[-1] == ("command", "failed", "inbound_unfinished")
    assert failure_replies(client)[unfinished] == "暂时无法处理，请稍后重新发送"
    client.portal.call(inbound_module.retry, runtime, runtime.messaging_vault())
    assert len(failure_replies(client)) == 4

    # Only the module whose command it was may answer, and only commands are answered.
    admin, answered = admin_id(client), inbound_of(client, "om_c3")

    async def foreign():
        identity = inbound_module.ActingUser(runtime, admin, "test")
        await reply_to(identity, answered, {"kind": "text", "text": "x"}, "other")

    with pytest.raises(ServiceError) as refused:
        client.portal.call(foreign)
    assert refused.value.code == "message_reply_unavailable"
    # The command text itself is never kept.
    assert "订阅" not in client.get(ROOT + "/inbounds").text


def str_id(client, inbound_id):
    rows = query(
        client,
        lambda session: session.scalars(
            select(MessageDelivery.id).where(MessageDelivery.reply_inbound_id == inbound_id)
        ),
    ).all()
    return str(rows[0])


def inbound_of(client, message_id):
    return str(
        query(
            client,
            lambda session: session.scalar(
                select(MessageInbound.id).where(MessageInbound.dedupe_key == message_id)
            ),
        )
    )


def admin_id(client):
    return UUID(client.get("/api/v1/auth/me").json()["user"]["id"])


def test_group_members_act_as_the_executing_user_the_delegator_could_grant(
    client, provider, monkeypatch
):
    channel = configure_app(client)
    group = add_group(client, channel)
    _, hook = configure(client)
    alice = create_user(client, "alice")

    async def activate(session):
        # A new user must change the password first and holds no permissions until then.
        await session.execute(
            update(User).where(User.id == UUID(alice["id"])).values(password_change_required=False)
        )

    query(client, activate)
    operators = role(client, "operators", ["messaging:manage"])
    assert assign(client, alice["id"], operators["id"]).status_code in (200, 204)
    seen = []

    async def status(identity, inbound_id, command_key):
        async with identity.access("messaging:manage") as (_, auth):
            seen.append(auth.user.username)
        await reply_to(
            identity,
            inbound_id,
            {
                "kind": "card",
                "title": "状态",
                "actions": [{"key": "confirm", "text": "确认"}],
            },
            source_type="status",
        )

    async def confirm(runtime, identity, source_id, action_key, inbound_id):
        async with identity.access("messaging:manage") as (_, auth):
            seen.append(("confirm", auth.user.username))

    declare(
        monkeypatch,
        client,
        commands=(
            MessageCommand(
                "reports",
                "status",
                "状态",
                ("group",),
                "messaging:manage",
                status,
                timedelta(minutes=1),
                identity="destination_user",
                reply_failures=True,
            ),
        ),
        actions=(
            MessageAction(
                "reports",
                "status",
                "confirm",
                "messaging:manage",
                confirm,
                identity="destination_user",
            ),
        ),
    )

    # No executing user yet: refused without a binding being needed, and told so.
    receive(client, channel, group_text("om_s1", "@_user_1 状态", open_id="ou_stranger"))
    assert inbounds(client)[-1] == ("command", "rejected", "message_executor_unset")
    assert "执行用户" in failure_replies(client)[UUID(inbound_of(client, "om_s1"))]

    # Setting one needs messaging:delegate and every permission of the user handed over.
    url = f"{ROOT}/destinations/{group['id']}/executor"
    manage_only = token(client, ["messaging:manage"])["token"]
    refused = client.put(url, json={"user_id": alice["id"]}, headers=bearer(manage_only))
    assert refused.json()["code"] == "permission_denied"
    delegate = token(client, ["messaging:manage", "messaging:delegate"])["token"]
    refused = client.put(url, json={"user_id": str(admin_id(client))}, headers=bearer(delegate))
    assert refused.status_code == 403
    hook_url = f"{ROOT}/destinations/{hook['id']}/executor"
    assert client.put(hook_url, json={"user_id": alice["id"]}).status_code == 422
    saved = client.put(url, json={"user_id": alice["id"]}, headers=bearer(delegate))
    assert saved.status_code == 200, saved.text
    assert saved.json()["executing_user"]["label"]
    audit = client.get("/api/v1/audit-logs?action=messaging.destination.delegate").json()
    assert audit[0]["details"]["executing_user_id"] == alice["id"]

    # Any member of the group, bound or not, now acts as alice.
    receive(client, channel, group_text("om_s2", "@_user_1 状态", open_id="ou_stranger"))
    assert inbounds(client)[-1] == ("command", "processed", None)
    assert seen == ["alice"]
    run_dispatch(client)
    message_id, card, delivery_id = provider.replies[-1]
    assert message_id == "om_s2" and card["kind"] == "card"
    # The reply card belongs to the same group, so its button also runs as alice.
    answer, _ = receive(
        client,
        channel,
        card_click("e1", delivery_id, open_id="ou_other", message_id="om_reply2", action="confirm"),
    )
    assert answer["toast"]["type"] == "success", answer
    assert seen[-1] == ("confirm", "alice")

    # Without the permission the command is refused without naming the executing user.
    assert assign(client, alice["id"], operators["id"], False).status_code in (200, 204)
    receive(client, channel, group_text("om_s3", "@_user_1 状态", open_id="ou_stranger"))
    assert inbounds(client)[-1] == ("command", "rejected", "permission_denied")
    text = failure_replies(client)[UUID(inbound_of(client, "om_s3"))]
    assert "alice" not in text and "权限" in text

    # Clearing the executing user stops it again.
    assert client.put(url, json={"user_id": None}).json()["executing_user"] is None


def test_command_declarations_are_checked_at_assembly():
    async def handler(identity, inbound_id, command_key):
        pass

    def command(key="a", text="订阅", where=("p2p",), accept_within=timedelta(minutes=1), **extra):
        return MessageCommand("reports", key, text, where, None, handler, accept_within, **extra)

    build_commands((command(), command("b", "退订", ("group",))), PERMISSIONS)
    for commands in (
        (command(), command("b")),
        (command(), command()),
        (command(text="绑定 订阅"),),
        (command(text=" 订阅"),),
        (command(where=()),),
        (command(where=("chat",)),),
        (command(identity="destination_user"),),
        (command(identity="sender"),),
        (command(accept_within=timedelta(0)),),
        (command(key="Bad"),),
    ):
        with pytest.raises(ValueError):
            build_commands(commands, PERMISSIONS)
    with pytest.raises(ValueError):
        build_commands(
            (MessageCommand("reports", "a", "x", ("p2p",), "x:y", handler, timedelta(1)),),
            PERMISSIONS,
        )


def test_supplied_content_is_fetched_when_sending_and_never_stored(client, provider, monkeypatch):
    channel = configure_app(client)
    add_group(client, channel)
    runtime = client.app.state.runtime
    secret = "https://tag.example/subscribe?token=never-stored"
    answers = {}

    async def link(identity, source_id):
        async with identity.access("messaging:manage") as (_, auth):
            assert auth.user.username == "admin"
        answer = answers[source_id]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runtime, "message_contents", {("reports", "link"): link})
    auth = SimpleNamespace(user=SimpleNamespace(id=admin_id(client)))

    def enqueue(source_id, source_type="link", **extra):
        async def register():
            async with runtime.database.transaction() as session:
                return await enqueue_in_transaction(
                    session,
                    auth,
                    destination_key="ops.app",
                    source_module="reports",
                    source_type=source_type,
                    source_id=source_id,
                    **({"supplied": "text"} | extra),
                )

        return client.portal.call(register)

    with pytest.raises(ValueError):
        enqueue("x", content={"kind": "text", "text": "both"})
    answers |= {
        "ok": {"kind": "text", "text": secret},
        "gone": None,
        "card": {"kind": "card", "title": "x", "actions": [{"key": "a", "text": "a"}]},
        "flaky": RuntimeError(secret),
        "refused": ServiceError(403, "permission_denied", "no"),
    }
    ok, gone, card, flaky, refused, missing = (
        enqueue("ok"),
        enqueue("gone"),
        enqueue("card"),
        enqueue("flaky"),
        enqueue("refused"),
        enqueue("x", source_type="other"),
    )
    run_dispatch(client)

    def outcome(delivery_id):
        return query(
            client,
            lambda session: session.get(MessageDelivery, delivery_id, populate_existing=True),
        )

    assert [content for *_, content, _ in provider.sent] == [{"kind": "text", "text": secret}]
    row = outcome(ok)
    assert (row.status, row.content_mode, row.content) == ("succeeded", "supplied", None)
    assert (outcome(gone).status, outcome(gone).error_code) == ("expired", "content_unavailable")
    assert outcome(card).error_code == "content_invalid"
    assert (outcome(refused).status, outcome(refused).error_code) == ("failed", "permission_denied")
    assert outcome(missing).error_code == "content_provider_missing"
    # Nothing reached Feishu, so the first attempt goes back to pending and is tried again.
    assert (outcome(flaky).status, outcome(flaky).error_code) == (
        "pending",
        "content_provider_failed",
    )
    answers["flaky"] = {"kind": "text", "text": "later"}
    run_dispatch(client)
    assert outcome(flaky).status == "succeeded"
    listed = client.get(ROOT + "/deliveries").text
    assert secret not in listed and '"content_mode":"supplied"' in listed.replace(" ", "")
