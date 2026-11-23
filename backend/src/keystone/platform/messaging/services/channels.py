"""Channel and destination configuration; credentials are write-only."""

import json

from pydantic import SecretStr
from sqlalchemy import delete, select

from keystone.core.errors import ServiceError
from keystone.platform.messaging.adapters import feishu_app, feishu_errors
from keystone.platform.messaging.models import (
    ExternalAccount,
    MessageChannel,
    MessageDelivery,
    MessageDestination,
    MessageInbound,
)
from keystone.platform.messaging.schemas import AppCredential, WebhookCredential
from keystone.platform.messaging.services.actions import TEST_CARD
from keystone.platform.messaging.services.deliveries import enqueue_in_transaction

TEST_TEXT = "Keystone 测试消息：渠道配置可用。"

PURPOSE = {"feishu_webhook": "messaging.webhook", "feishu_app": "messaging.app"}
CREDENTIAL = {"feishu_webhook": WebhookCredential, "feishu_app": AppCredential}


def credential_unavailable():
    return ServiceError(503, "message_credential_unavailable", "消息渠道凭据或加密密钥不可用")


def channel_view(row, destination=None):
    return {
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "name",
                "provider",
                "app_id",
                "is_active",
                "created_at",
                "created_by",
                "updated_at",
                "updated_by",
            )
        },
        "has_credential": bool(row.credential_encrypted),
        "destination": destination
        and {key: getattr(destination, key) for key in ("id", "key", "is_active")},
    }


def destination_view(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "key",
            "channel_id",
            "external_id",
            "name",
            "is_active",
            "created_at",
            "updated_at",
        )
    }


async def credentials_stored(session):
    """Whether any message channel holds a sealed credential; used by composition."""
    return (
        await session.scalar(
            select(MessageChannel.id).where(MessageChannel.credential_encrypted != "").limit(1)
        )
        is not None
    )


async def seal(session, vault, row, credential):
    if not isinstance(credential, CREDENTIAL[row.provider]):
        raise ServiceError(422, "message_credential_invalid", "凭据与渠道类型不匹配")
    if isinstance(credential, AppCredential):
        value = {"app_secret": credential.app_secret.get_secret_value()}
    else:
        value = {"url": credential.url.get_secret_value()}
        if credential.secret is not None:
            value["secret"] = credential.secret.get_secret_value()
    return vault.encrypt(
        SecretStr(json.dumps(value)),
        row.id,
        PURPOSE[row.provider],
        create=await vault.may_create(session),
    )


def unseal(vault, row):
    return json.loads(vault.decrypt(row.credential_encrypted, row.id, PURPOSE[row.provider]))


async def remove_deliveries(session, condition):
    """Delete matching delivery history; a delivery being sent blocks the whole removal."""
    # A row claimed meanwhile is re-checked by PostgreSQL and survives as `sending`.
    removed = await session.execute(
        delete(MessageDelivery).where(condition, MessageDelivery.status != "sending")
    )
    if await session.scalar(select(MessageDelivery.id).where(condition).limit(1)):
        raise ServiceError(409, "message_channel_sending", "有消息正在发送，请稍后再删除")
    return removed.rowcount


class ChannelService:
    def __init__(self, identity, vault):
        self.identity, self.vault = identity, vault

    async def channels(self, offset, limit):
        async with self.identity.access("messaging:manage") as (session, _):
            rows = (
                await session.scalars(
                    select(MessageChannel)
                    .order_by(MessageChannel.name, MessageChannel.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            # Shown only for group bots, whose single destination is created with the channel.
            destinations = {
                row.channel_id: row
                for row in await session.scalars(
                    select(MessageDestination).where(
                        MessageDestination.channel_id.in_(
                            [row.id for row in rows if row.provider == "feishu_webhook"]
                        )
                    )
                )
            }
            return [channel_view(row, destinations.get(row.id)) for row in rows]

    async def save_channel(self, data, identifier=None):
        action = f"messaging.channel.{'update' if identifier else 'create'}"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.channel", target_id=identifier
        ) as (session, auth):
            if identifier:
                row = await session.get(MessageChannel, identifier, with_for_update=True)
                if row is None:
                    raise ServiceError(404, "message_channel_not_found", "消息渠道不存在")
                row.name, row.is_active = data.name, data.is_active
                self.identity.touch(row, auth)
                destination = None
                if row.provider == "feishu_webhook":
                    destination = await session.scalar(
                        select(MessageDestination).where(MessageDestination.channel_id == row.id)
                    )
                if destination is not None:
                    destination.name = row.name
                    self.identity.touch(destination, auth)
            else:
                row = MessageChannel(
                    name=data.name,
                    provider=data.provider,
                    app_id=getattr(data, "app_id", None),
                    credential_encrypted="",
                    is_active=True,
                    created_by=auth.user.id,
                    updated_by=auth.user.id,
                )
                session.add(row)
                await session.flush()
                destination = None
                if row.provider == "feishu_webhook":
                    destination = MessageDestination(
                        key=data.destination_key,
                        channel_id=row.id,
                        name=row.name,
                        is_active=True,
                        created_by=auth.user.id,
                        updated_by=auth.user.id,
                    )
                    session.add(destination)
            if data.credential is not None:
                row.credential_encrypted = await seal(session, self.vault, row, data.credential)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.channel",
                row.id,
                {
                    "name": row.name,
                    "provider": row.provider,
                    "app_id": row.app_id,
                    "is_active": row.is_active,
                    "credential_changed": data.credential is not None,
                    "destination_key": destination and destination.key,
                },
            )
            return channel_view(row, destination)

    async def delete_channel(self, identifier):
        """Remove a channel with its destinations and delivery history.

        Deliveries are operational records owned by the channel, so they go with it; pending
        ones are simply never sent. Only a delivery being sent right now blocks the removal.
        """
        action = "messaging.channel.delete"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.channel", target_id=identifier
        ) as (session, auth):
            row = await session.get(MessageChannel, identifier, with_for_update=True)
            if row is None:
                raise ServiceError(404, "message_channel_not_found", "消息渠道不存在")
            removed = await remove_deliveries(session, MessageDelivery.channel_id == row.id)
            await session.execute(
                delete(ExternalAccount).where(ExternalAccount.channel_id == row.id)
            )
            await session.execute(delete(MessageInbound).where(MessageInbound.channel_id == row.id))
            keys = (
                await session.scalars(
                    delete(MessageDestination)
                    .where(MessageDestination.channel_id == row.id)
                    .returning(MessageDestination.key)
                )
            ).all()
            details = {"name": row.name, "provider": row.provider}
            await session.delete(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.channel",
                identifier,
                {
                    **details,
                    "destination_keys": sorted(keys),
                    "deliveries_removed": removed,
                },
            )

    async def chats(self, identifier, page_token=None):
        """One page of the groups an app bot is in, read live from Feishu."""
        async with self.identity.access("messaging:manage") as (session, _):
            row = await session.get(MessageChannel, identifier)
            if row is None:
                raise ServiceError(404, "message_channel_not_found", "消息渠道不存在")
            if row.provider != "feishu_app":
                raise ServiceError(422, "message_channel_unsupported", "只有自建应用渠道可选择群")
            app_id, secret = row.app_id, unseal(self.vault, row)["app_secret"]
        # The provider call happens outside the database transaction.
        try:
            return await feishu_app.chats(app_id, secret, page_token)
        except (feishu_app.Rejected, feishu_app.NotSent) as error:
            reason = feishu_errors.hint(error.code) or "飞书拒绝了请求"
            raise ServiceError(
                502, "message_provider_rejected", f"{reason}（{error.code}）"
            ) from None
        except feishu_app.Unknown:
            raise ServiceError(504, "message_provider_unavailable", "飞书暂时无法访问") from None

    async def destinations(self, offset, limit, provider=None):
        async with self.identity.access("messaging:manage") as (session, _):
            query = select(MessageDestination)
            if provider is not None:
                query = query.join(
                    MessageChannel, MessageDestination.channel_id == MessageChannel.id
                ).where(MessageChannel.provider == provider)
            rows = await session.scalars(
                query.order_by(MessageDestination.key).offset(offset).limit(limit)
            )
            return [destination_view(row) for row in rows]

    async def save_destination(self, data, identifier=None):
        action = f"messaging.destination.{'update' if identifier else 'create'}"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.destination", target_id=identifier
        ) as (session, auth):
            if identifier:
                row = await session.get(MessageDestination, identifier, with_for_update=True)
                if row is None:
                    raise ServiceError(404, "message_destination_not_found", "消息接收方不存在")
                row.name, row.is_active = data.name, data.is_active
                self.identity.touch(row, auth)
            else:
                channel = await session.get(MessageChannel, data.channel_id, with_for_update=True)
                if channel is None:
                    raise ServiceError(404, "message_channel_not_found", "消息渠道不存在")
                if channel.provider == "feishu_webhook":
                    # A group bot reaches exactly the group it was added to.
                    taken = await session.scalar(
                        select(MessageDestination.id).where(
                            MessageDestination.channel_id == channel.id
                        )
                    )
                    if data.external_id is not None or taken is not None:
                        raise ServiceError(
                            422,
                            "message_destination_invalid",
                            "群机器人渠道只有一个接收方，且不填外部会话标识",
                        )
                elif data.external_id is None:
                    raise ServiceError(
                        422, "message_destination_invalid", "自建应用渠道的接收方须选择群"
                    )
                row = MessageDestination(
                    key=data.key,
                    channel_id=channel.id,
                    external_id=data.external_id,
                    name=data.name,
                    is_active=True,
                    created_by=auth.user.id,
                    updated_by=auth.user.id,
                )
                session.add(row)
                await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.destination",
                row.id,
                {
                    "key": row.key,
                    "channel_id": str(row.channel_id),
                    "name": row.name,
                    "is_active": row.is_active,
                },
            )
            return destination_view(row)

    async def delete_destination(self, identifier):
        """Remove one destination of an app channel together with its delivery history."""
        action = "messaging.destination.delete"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.destination", target_id=identifier
        ) as (session, auth):
            row = await session.get(MessageDestination, identifier)
            if row is None:
                raise ServiceError(404, "message_destination_not_found", "消息接收方不存在")
            # Same lock order as channel deletion: the channel, then its deliveries.
            channel = await session.get(MessageChannel, row.channel_id, with_for_update=True)
            if channel.provider == "feishu_webhook":
                raise ServiceError(
                    422, "message_destination_fixed", "群机器人的消息标识随渠道一起删除"
                )
            removed = await remove_deliveries(session, MessageDelivery.destination_id == row.id)
            details = {"key": row.key, "channel_id": str(row.channel_id), "name": row.name}
            await session.delete(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.destination",
                identifier,
                {**details, "deliveries_removed": removed},
            )

    async def test(self, identifier, *, card=False):
        """Queue a fixed test notice to one destination; the Worker sends it."""
        action = "messaging.destination.test"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.destination", target_id=identifier
        ) as (session, auth):
            row = await session.get(MessageDestination, identifier)
            if row is None:
                raise ServiceError(404, "message_destination_not_found", "消息接收方不存在")
            delivery = await enqueue_in_transaction(
                session,
                auth,
                destination_key=row.key,
                content=TEST_CARD if card else {"kind": "text", "text": TEST_TEXT},
                source_module="messaging",
                source_type="destination_test",
                source_id=str(row.id),
            )
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.destination",
                row.id,
                {"delivery_id": str(delivery)},
            )
            return {"delivery_id": delivery}
