from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.adapters.channels import CHANNELS, PROVIDERS
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation
from keystone.platform.model_access.services.cost_context import bind_channel
from keystone.platform.model_access.services.queries import balance_config, locked


class ChannelService:
    def __init__(self, identity: AccessContext, clients):
        self.identity, self.clients = identity, clients

    async def options(self):
        async with self.identity.access("model:manage"):
            return {
                "providers": list(PROVIDERS),
                "default_api_bases": {key: CHANNELS[key].api_base for key in PROVIDERS},
                "balance_providers": [
                    key
                    for key, adapter in CHANNELS.items()
                    if getattr(adapter.billing, "enabled", False)
                    and getattr(adapter.billing, "balance_path", None)
                ],
                "bill_providers": [
                    key for key, adapter in CHANNELS.items() if hasattr(adapter.billing, "bills")
                ],
            }

    async def upstream_models(self, identifier):
        async with self.clients.coordination.balance_slots:
            async with self.identity.access("model:manage") as (session, _):
                row = await self.identity.existing(session, Channel, identifier)
                config = (row.provider, row.api_base, row.api_key_encrypted)
            provider, base, encrypted = config
            secret = self.clients.credentials.decrypt(encrypted, identifier, "inference")
            items = await self.clients.inference.list_models(provider, base, secret)
            async with self.identity.access("model:manage") as (session, _):
                row = await self.identity.existing(session, Channel, identifier)
                if (row.provider, row.api_base, row.api_key_encrypted) != config:
                    raise ServiceError(
                        409, "model_configuration_changed", "渠道配置已改变，请重新查询模型列表"
                    )
                return {"items": items}

    async def update_keys(self, session, row, data):
        if data.api_key is None and not row.api_key_encrypted:
            raise ServiceError(422, "model_api_key_required", "请填写渠道 API Key")
        billing_key = data.billing_api_key
        if data.billing_credentials is not None:
            if row.provider not in {"aliyun_bailian", "volcengine"}:
                raise ServiceError(
                    422, "model_billing_credentials_invalid", "当前渠道不使用云账务凭据"
                )
            billing_key = data.billing_credentials.encoded()
        if data.api_key is not None or billing_key is not None:
            existing = await session.scalar(
                select(Channel.id)
                .where(
                    or_(
                        Channel.api_key_encrypted.is_not(None),
                        Channel.billing_api_key_encrypted.is_not(None),
                    )
                )
                .limit(1)
            )
            for field, purpose, value in (
                ("api_key", "inference", data.api_key),
                ("billing_api_key", "billing", billing_key),
            ):
                if value is not None:
                    encrypted = self.clients.credentials.encrypt(
                        value, row.id, purpose, create=existing is None
                    )
                    setattr(row, field + "_encrypted", encrypted)
        if data.clear_billing_api_key:
            row.billing_api_key_encrypted = None

    @staticmethod
    def snapshot(row, fields):
        return {
            **{key: getattr(row, key) for key in fields},
            "has_api_key": bool(row.api_key_encrypted),
            "has_billing_api_key": bool(row.billing_api_key_encrypted),
        }

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
            values = data.model_dump(
                exclude={
                    "api_key",
                    "billing_api_key",
                    "billing_credentials",
                    "clear_billing_api_key",
                }
            )
            if identifier:
                row = await locked(session, Channel, identifier)
                if self.clients.billing.adapter(
                    row.provider, row.api_base
                ).cost_query_mode == "logs" and (
                    data.api_base != row.api_base
                    or (
                        data.api_key is not None
                        and data.api_key.get_secret_value()
                        != self.clients.credentials.decrypt(
                            row.api_key_encrypted, row.id, "inference"
                        )
                    )
                ):
                    raise ServiceError(
                        409,
                        "channel_query_scope_fixed",
                        "换站点或 Token 请新建渠道，原渠道保留历史查费凭据",
                    )
                if (
                    row.upstream_account_ref is not None
                    and row.upstream_account_ref != data.upstream_account_ref
                ):
                    raise ServiceError(
                        409, "channel_account_fixed", "账户归属创建后固定；换账户请新建渠道"
                    )
                before = self.snapshot(row, values)
                previous_config = balance_config(row)
                await self.update_keys(session, row, data)
                for key, value in values.items():
                    setattr(row, key, value)
                if balance_config(row) != previous_config:
                    row.balance_snapshot = row.balance_checked_at = row.balance_last_attempt_at = (
                        None
                    )
                    row.balance_check_status = "never"
                self.identity.touch(row, auth)
            else:
                before = None
                row = Channel(
                    id=uuid4(), **values, created_by=auth.user.id, updated_by=auth.user.id
                )
                await self.update_keys(session, row, data)
                session.add(row)
            bind_channel(row, self.clients)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "model.channel",
                row.id,
                {
                    "before": before,
                    "after": self.snapshot(row, values),
                    "api_key_changed": data.api_key is not None,
                    "billing_api_key_changed": data.billing_api_key is not None
                    or data.billing_credentials is not None
                    or data.clear_billing_api_key,
                },
            )
            return schemas.channel_view(row)

    async def cloud_bills(self, identifier, data):
        # Keep manual accounting queries within a bounded two-year window.
        year, month = map(int, data.period.split("-"))
        current = utcnow() + timedelta(hours=8)
        age = (current.year - year) * 12 + current.month - month
        if not 0 <= age <= 24:
            raise ServiceError(
                422, "model_billing_period_invalid", "账期须在当前月份及之前24个月内"
            )
        async with self.clients.coordination.balance_slots:
            async with self.identity.access("model:manage") as (session, _):
                row = await self.identity.existing(session, Channel, identifier)
                if not hasattr(self.clients.billing.adapter(row.provider, row.api_base), "bills"):
                    raise ServiceError(
                        409, "model_billing_unavailable", "当前渠道未接入云账户月账单"
                    )
                config = balance_config(row)
            provider, base, encrypted, _ = config
            if not encrypted:
                raise ServiceError(409, "model_billing_credentials_required", "请先配置云账务凭据")
            secret = self.clients.credentials.decrypt(encrypted, identifier, "billing")
            report = await self.clients.billing.bills(
                provider, base, secret, data.period, data.offset
            )
            async with self.identity.access("model:manage") as (session, _):
                row = await self.identity.existing(session, Channel, identifier)
                if balance_config(row) != config:
                    raise ServiceError(
                        409, "model_configuration_changed", "账务配置已改变，请重新查询"
                    )
            return {
                "scope": "cloud_account",
                "period": data.period,
                "offset": data.offset,
                "limit": 50,
                "fetched_at": utcnow().isoformat(),
                **report,
            }

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

    async def rename(self, identifier, data):
        async with self.identity.access(
            "model:manage",
            action="model.channel.update",
            target="model.channel",
            target_id=identifier,
        ) as (session, auth):
            row = await locked(session, Channel, identifier)
            before = row.name
            row.name = data.name
            self.identity.touch(row, auth)
            await self.identity.log(
                session,
                auth,
                "model.channel.update",
                "model.channel",
                row.id,
                {"before": {"name": before}, "after": {"name": row.name}},
            )
            return schemas.channel_view(row)

    async def delete(self, identifier):
        async with self.identity.access(
            "model:manage",
            action="model.channel.delete",
            target="model.channel",
            target_id=identifier,
        ) as (session, auth):
            row = await locked(session, Channel, identifier)
            configured = await session.scalar(
                select(ModelEntry.id).where(ModelEntry.channel_id == identifier).limit(1)
            )
            if configured:
                raise ServiceError(
                    409,
                    "model_channel_referenced",
                    "渠道仍有模型配置，请先删除未使用的模型；有引用时只能停用",
                )
            called = await session.scalar(
                select(ModelInvocation.id).where(ModelInvocation.channel_id == identifier).limit(1)
            )
            if called or await self.clients.model_referenced(session, channel_id=identifier):
                raise ServiceError(
                    409,
                    "model_channel_referenced",
                    "渠道已有调用记录或流程引用，只能停用，不能删除",
                )
            await self.identity.log(
                session,
                auth,
                "model.channel.delete",
                "model.channel",
                row.id,
                {"name": row.name, "provider": row.provider},
            )
            await session.delete(row)

    async def _refresh_balance(self, identifier, actor, audit_details):
        action = "model.balance.refresh"
        async with (
            self.clients.coordination.balance_slots,
            self.clients.coordination.scope(identifier) as still_owned,
        ):
            async with self.identity.access("model:manage") as (session, _):
                channel = await session.get(Channel, identifier)
                config = balance_config(channel)
            provider, base, encrypted, _ = config
            adapter = self.clients.billing.adapter(provider, base)
            attempted = utcnow()
            snapshot = None
            status = "unavailable"
            try:
                if encrypted:
                    secret = self.clients.credentials.decrypt(
                        encrypted,
                        identifier,
                        "billing" if adapter.uses_billing_key else "inference",
                    )
                    snapshot = await self.clients.billing.balance(provider, base, secret)
                    status = "ok"
            except Exception as error:
                status = (
                    "unavailable"
                    if isinstance(error, ServiceError)
                    and error.code
                    in {
                        "model_credential_unavailable",
                        "model_billing_credentials_required",
                        "model_provider_unavailable",
                        "model_target_not_allowed",
                    }
                    else "error"
                )
            async with still_owned.transaction() as session:
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
