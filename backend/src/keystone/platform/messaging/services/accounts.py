"""External account bindings: which Feishu user a Keystone user is on one app channel.

Administrators bind by the user's Feishu email or mobile; only the resulting open ID is kept,
never the email or mobile used to look it up.
"""

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.identity.services.subjects import users_in_transaction
from keystone.platform.messaging.adapters import feishu_app, feishu_errors
from keystone.platform.messaging.models import ExternalAccount, MessageChannel
from keystone.platform.messaging.services.actions import TEST_CARD
from keystone.platform.messaging.services.channels import unseal
from keystone.platform.messaging.services.deliveries import register, user_is_active

TEST_TEXT = "Keystone 测试消息：飞书账号绑定可用。"


def provider_error(error):
    reason = feishu_errors.hint(error.code) or "飞书拒绝了请求"
    return ServiceError(502, "message_provider_rejected", f"{reason}（{error.code}）")


def account_view(row, label=None):
    return {
        "id": row.id,
        "channel_id": row.channel_id,
        "user": {"id": row.user_id, "label": label},
        "external_user_id": row.external_user_id,
        "created_at": row.created_at,
    }


async def app_channel(session, identifier, *, lock=False):
    row = await session.get(MessageChannel, identifier, with_for_update=lock)
    if row is None:
        raise ServiceError(404, "message_channel_not_found", "消息渠道不存在")
    if row.provider != "feishu_app":
        raise ServiceError(422, "message_channel_unsupported", "只有自建应用渠道可绑定账号")
    return row


class AccountService:
    def __init__(self, identity, vault):
        self.identity, self.vault = identity, vault

    async def accounts(self, offset, limit):
        async with self.identity.access("messaging:manage") as (session, _):
            rows = (
                await session.scalars(
                    select(ExternalAccount)
                    .order_by(ExternalAccount.created_at.desc(), ExternalAccount.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            labels = {
                user["id"]: user["label"]
                for user in await users_in_transaction(session, {row.user_id for row in rows})
            }
            return [account_view(row, labels.get(row.user_id)) for row in rows]

    async def _credential(self, data, user_id=None):
        async with self.identity.access("messaging:manage") as (session, _):
            channel = await app_channel(session, data.channel_id)
            if user_id is not None and not await users_in_transaction(session, [user_id]):
                raise ServiceError(404, "user_not_found", "用户不存在")
            return channel.app_id, unseal(self.vault, channel)["app_secret"]

    async def _open_id(self, app_id, secret, data):
        """Look the contact up outside any transaction; the contact itself is not kept."""
        try:
            open_id = await feishu_app.open_id(app_id, secret, email=data.email, mobile=data.mobile)
        except (feishu_app.Rejected, feishu_app.NotSent) as error:
            raise provider_error(error) from None
        except feishu_app.Unknown:
            raise ServiceError(504, "message_provider_unavailable", "飞书暂时无法访问") from None
        if open_id is None:
            raise ServiceError(
                422,
                "message_account_not_found",
                "飞书中没有找到这个邮箱或手机号对应的用户；企业邮箱无法查询，"
                "用户也须在应用的通讯录权限范围内",
            )
        return open_id

    async def lookup(self, data):
        """Preview who a contact belongs to before binding; nothing is stored or audited."""
        app_id, secret = await self._credential(data)
        open_id = await self._open_id(app_id, secret, data)
        name, name_hint = None, None
        try:
            name = await feishu_app.user_name(app_id, secret, open_id)
        except (feishu_app.Rejected, feishu_app.NotSent):
            name_hint = "开通“获取用户基本信息”（contact:user.base:readonly）后可显示姓名"
        except feishu_app.Unknown:
            name_hint = "暂时无法读取姓名"
        async with self.identity.access("messaging:manage") as (session, _):
            user_id = await session.scalar(
                select(ExternalAccount.user_id).where(
                    ExternalAccount.channel_id == data.channel_id,
                    ExternalAccount.external_user_id == open_id,
                )
            )
            found = await users_in_transaction(session, [user_id]) if user_id else []
        return {
            "external_user_id": open_id,
            "name": name,
            "name_hint": name_hint,
            "bound_to": found[0] if found else None,
        }

    async def bind(self, data):
        # Resolved again here: a previewed open ID from the browser is never trusted.
        app_id, secret = await self._credential(data, data.user_id)
        open_id = await self._open_id(app_id, secret, data)
        action = "messaging.account.create"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.account"
        ) as (session, auth):
            channel = await app_channel(session, data.channel_id, lock=True)
            existing = await session.scalar(
                select(ExternalAccount).where(
                    ExternalAccount.channel_id == channel.id,
                    (ExternalAccount.user_id == data.user_id)
                    | (ExternalAccount.external_user_id == open_id),
                )
            )
            if existing is not None:
                raise ServiceError(
                    409,
                    "message_account_exists",
                    "该用户在此渠道已绑定"
                    if existing.user_id == data.user_id
                    else "这个飞书账号已绑定到其他用户",
                )
            row = ExternalAccount(
                channel_id=channel.id,
                external_user_id=open_id,
                user_id=data.user_id,
                created_by=auth.user.id,
            )
            session.add(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.account",
                row.id,
                {
                    "channel_id": str(row.channel_id),
                    "user_id": str(row.user_id),
                    "external_user_id": open_id,
                },
            )
            [user] = await users_in_transaction(session, [row.user_id])
            return account_view(row, user["label"])

    async def unbind(self, identifier):
        """Pending deliveries to this user on the channel fail when sent; none is redirected."""
        action = "messaging.account.delete"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.account", target_id=identifier
        ) as (session, auth):
            row = await session.get(ExternalAccount, identifier, with_for_update=True)
            if row is None:
                raise ServiceError(404, "message_account_not_found", "账号绑定不存在")
            details = {
                "channel_id": str(row.channel_id),
                "user_id": str(row.user_id),
                "external_user_id": row.external_user_id,
            }
            await session.delete(row)
            await session.flush()
            await self.identity.log(session, auth, action, "messaging.account", identifier, details)

    async def test(self, identifier, *, card=False):
        """Queue a fixed notice to one bound user on that binding's channel."""
        action = "messaging.account.test"
        async with self.identity.access(
            "messaging:manage", action=action, target="messaging.account", target_id=identifier
        ) as (session, auth):
            row = await session.get(ExternalAccount, identifier)
            if row is None:
                raise ServiceError(404, "message_account_not_found", "账号绑定不存在")
            channel = await app_channel(session, row.channel_id)
            if not channel.is_active:
                raise ServiceError(409, "message_destination_unavailable", "渠道已停用")
            if not await user_is_active(session, row.user_id):
                raise ServiceError(409, "message_recipient_disabled", "该用户已停用")
            delivery = await register(
                session,
                auth,
                channel.id,
                TEST_CARD if card else {"kind": "text", "text": TEST_TEXT},
                {
                    "source_module": "messaging",
                    "source_type": "account_test",
                    "source_id": str(row.id),
                },
                None,
                recipient_user_id=row.user_id,
            )
            await self.identity.log(
                session,
                auth,
                action,
                "messaging.account",
                row.id,
                {"delivery_id": str(delivery)},
            )
            return {"delivery_id": delivery}
