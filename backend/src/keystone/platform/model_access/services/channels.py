from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.gateway import refresh_lock
from keystone.platform.model_access.models import Channel
from keystone.platform.model_access.providers import PROVIDERS
from keystone.platform.model_access.services.queries import balance_config, locked


class ChannelService:
    def __init__(self, identity: AccessContext, gateway):
        self.identity, self.gateway = identity, gateway

    async def options(self):
        async with self.identity.access("model:manage"):
            return {
                "mode": self.gateway.settings.model_mode,
                "credential_refs": sorted(self.gateway.settings.model_secrets),
                "providers": list(PROVIDERS),
            }

    def validate_refs(self, data):
        for reference in (data.credential_ref, data.billing_credential_ref):
            if reference is not None and reference not in self.gateway.settings.model_secrets:
                raise ServiceError(422, "unknown_secret_reference", "请选择已配置的秘密引用")

    async def channels(self, offset, limit):
        async with self.identity.access("model:manage") as (session, _):
            return [
                schemas.channel_view(row)
                for row in await session.scalars(
                    select(Channel)
                    .order_by(Channel.created_at.desc(), Channel.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    async def save_channel(self, data, identifier=None):
        action = "model.channel.update" if identifier else "model.channel.create"
        async with self.identity.access(
            "model:manage", action=action, target="model.channel", target_id=identifier
        ) as (session, auth):
            self.validate_refs(data)
            if identifier:
                row = await locked(session, Channel, identifier)
                if (
                    row.upstream_account_ref is not None
                    and row.upstream_account_ref != data.upstream_account_ref
                ):
                    raise ServiceError(
                        409, "channel_account_fixed", "账户归属创建后固定；换账户请新建渠道"
                    )
                before = {key: getattr(row, key) for key in data.model_dump()}
                previous_config = balance_config(row)
                for key, value in data.model_dump().items():
                    setattr(row, key, value)
                if balance_config(row) != previous_config:
                    row.balance_snapshot = row.balance_checked_at = row.balance_last_attempt_at = (
                        None
                    )
                    row.balance_check_status = "never"
                self.identity.touch(row, auth)
            else:
                before = None
                row = Channel(**data.model_dump(), created_by=auth.user.id, updated_by=auth.user.id)
                session.add(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "model.channel",
                row.id,
                {"before": before, "after": data.model_dump()},
            )
            return schemas.channel_view(row)

    async def refresh_balance(self, identifier):
        action = "model.balance.refresh"
        async with self.identity.access(
            "model:manage", action=action, target="model.channel", target_id=identifier
        ) as (session, auth):
            await self.identity.existing(session, Channel, identifier)
            actor = Actor("user", auth.user.id)
            audit_details = {"credential_id": str(auth.credential.id)} if auth.kind == "pat" else {}
        try:
            return await self._refresh_balance(identifier, actor, audit_details)
        except Exception as error:
            await audit.failed(
                self.identity.database,
                action,
                actor,
                "model.channel",
                identifier,
                self.identity.correlation_id,
                error.code if isinstance(error, ServiceError) else "internal_error",
                isinstance(error, ServiceError) and error.status in {401, 403},
                credential_id=audit_details.get("credential_id"),
            )
            raise

    async def _refresh_balance(self, identifier, actor, audit_details):
        action = "model.balance.refresh"
        async with (
            self.gateway.balance_slots,
            refresh_lock(self.identity.database, identifier) as still_owned,
        ):
            async with self.identity.access("model:manage") as (session, _):
                channel = await session.get(Channel, identifier)
                config = balance_config(channel)
            provider, base, reference, _ = config
            adapter = PROVIDERS[provider]
            attempted = utcnow()
            snapshot = None
            status = "unavailable"
            try:
                if reference:
                    secret = self.gateway.secret(reference)
                    payload = await self.gateway.query(provider, base, adapter.balance_path, secret)
                    snapshot, status = adapter.balance(payload), "ok"
            except Exception as error:
                status = (
                    "unavailable"
                    if isinstance(error, ServiceError)
                    and error.code
                    in {
                        "model_credential_unavailable",
                        "model_transport_disabled",
                        "model_provider_unavailable",
                        "model_target_not_allowed",
                    }
                    else "error"
                )
            await still_owned()
            async with self.identity.database.transaction() as session:
                channel = await locked(session, Channel, identifier)
                if balance_config(channel) != config:
                    raise ServiceError(
                        409, "model_configuration_changed", "查询配置已改变，旧余额结果已丢弃"
                    )
                channel.balance_last_attempt_at, channel.balance_check_status = attempted, status
                if status == "ok":
                    channel.balance_snapshot, channel.balance_checked_at = snapshot, utcnow()
                await audit.record(
                    session,
                    action,
                    actor,
                    "model.channel",
                    identifier,
                    outcome="success" if status == "ok" else "failure",
                    correlation_id=self.identity.correlation_id,
                    details={**audit_details, "status": status},
                )
            # Data preservation is allowed after revocation; returning management data is not.
            async with self.identity.access("model:manage") as (session, _):
                return schemas.channel_view(await session.get(Channel, identifier))
