import asyncio
import hashlib
from collections.abc import Callable
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.files.services.references import FileReferences
from keystone.platform.flow.models import TERMINAL, FlowRun
from keystone.platform.flow.schemas import view
from keystone.platform.flow.schemas.files import file_ids
from keystone.platform.flow.schemas.values import small_json
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.model_access.services.entries import select_model


def scope(auth):
    auth.require("flow:read")
    return [] if "flow:manage" in auth.permissions else [FlowRun.actor_id == auth.user.id]


class FlowService:
    def __init__(
        self, identity: AccessContext, definitions, *, execution_version: Callable[[], str]
    ):
        self.identity = identity
        self.flow_definitions = definitions
        self.execution_version = execution_version

    async def definitions(self):
        async with self.identity.access("flow:start") as (_, auth):
            return {
                "enabled": self.identity.settings.flow_enabled,
                "execution_version": self.execution_version(),
                "idempotency_days": 7,
                "items": [
                    d.view()
                    for d in self.flow_definitions.values()
                    if set(d.required_permissions) <= auth.permissions
                ],
            }

    async def start(self, data):
        async with self.identity.access(action="flow.start", target="flow.run") as (session, auth):
            return await self.start_in_transaction(session, auth, data)

    async def start_in_transaction(self, session, auth, data):
        """Trusted caller holds AccessContext.access() and commits business facts together.

        Uses the current authorization under its identity lock; never commits, opens
        another transaction or performs external I/O. Worker discovers pending rows.
        """
        small_json(data.input)
        pat_id = auth.credential.id if auth.kind == "pat" else None
        digest = hashlib.sha256(
            small_json([data.definition_key, data.input, str(pat_id) if pat_id else None])
        ).hexdigest()
        lock = int.from_bytes(
            hashlib.sha256(f"{auth.user.id}:{data.request_key}".encode()).digest()[:4],
            signed=True,
        )
        await session.execute(text("SELECT pg_advisory_xact_lock(73190423, :key)"), {"key": lock})
        old = await session.scalar(
            select(FlowRun).where(
                FlowRun.actor_id == auth.user.id, FlowRun.request_key == data.request_key
            )
        )
        if old:
            if old.request_digest != digest:
                raise ServiceError(409, "idempotency_conflict", "同一请求键对应不同流程请求")
            auth.require("flow:read")
            return view(old)
        auth.require("flow:start")
        if not self.identity.settings.flow_enabled:
            raise ServiceError(503, "flow_disabled", "Flow 尚未启用")
        definition = self.flow_definitions.get(data.definition_key)
        if definition is None:
            raise ServiceError(404, "definition_unavailable", "流程定义不可用")
        for permission in definition.required_permissions:
            auth.require(permission)
        try:
            inputs = definition.input_type.model_validate(data.input).model_dump(
                mode="json", by_alias=False
            )
        except ValidationError:
            raise ServiceError(422, "flow_invalid_input", "输入不符合流程契约") from None
        try:
            identifiers = file_ids(inputs, definition.file_fields)
        except ValueError:
            raise ServiceError(422, "flow_invalid_input", "输入文件 ID 无效或数量超限") from None
        if definition.model_field:
            try:
                selected = await select_model(session, inputs[definition.model_field], lock=True)
            except ServiceError as error:
                if error.code in {"model_not_found", "model_disabled"}:
                    raise ServiceError(409, "model_disabled", "模型或渠道不可用") from None
                raise
            inputs[definition.model_field] = selected.model_dump(mode="json")
        small_json(inputs)
        run = FlowRun(
            id=uuid4(),
            definition_key=definition.key,
            definition_snapshot=definition.snapshot(),
            execution_version=self.execution_version(),
            actor_id=auth.user.id,
            personal_access_token_id=pat_id,
            request_key=data.request_key,
            request_digest=digest,
            input=inputs,
        )
        session.add(run)
        await session.flush()
        await FileReferences(
            session, auth, "flow", correlation_id=self.identity.correlation_id
        ).replace(str(run.id), "input", identifiers)
        await self.identity.log(
            session, auth, "flow.start", "flow.run", run.id, {"definition_key": definition.key}
        )
        return view(run)

    async def listing(self, offset, limit, status=None):
        async with self.identity.access("flow:read") as (session, auth):
            filters = scope(auth) + ([FlowRun.status == status] if status else [])
            rows = await session.scalars(
                select(FlowRun)
                .where(*filters)
                .order_by(FlowRun.created_at.desc(), FlowRun.id)
                .offset(offset)
                .limit(limit)
            )
            return [view(row) for row in rows]

    async def detail(self, identifier):
        async with self.identity.access("flow:read") as (session, auth):
            return view(await self.visible(session, auth, identifier))

    async def visible(self, session, auth, identifier, *, lock=False):
        stmt = select(FlowRun).where(FlowRun.id == identifier, *scope(auth))
        run = await session.scalar(stmt.with_for_update() if lock else stmt)
        if run is None:
            raise ServiceError(404, "flow_not_found", "流程不存在或不可访问")
        return run

    async def cancel(self, identifier):
        async with self.identity.access(
            "flow:cancel", action="flow.cancel", target="flow.run", target_id=identifier
        ) as (session, auth):
            run = await self.visible(session, auth, identifier, lock=True)
            if run.status not in TERMINAL and run.cancel_requested_at is None:
                run.cancel_requested_at = utcnow()
                await self.identity.log(session, auth, "flow.cancel", "flow.run", run.id, {})
            return view(run)

    async def steps(self, identifier, engine):
        await self.detail(identifier)
        if engine is None:
            raise ServiceError(503, "flow_engine_unavailable", "执行历史暂不可用")
        try:
            async with asyncio.timeout(10):
                result = await engine.steps(identifier)
        except Exception:
            raise ServiceError(503, "flow_engine_unavailable", "执行历史暂不可用") from None
        await self.detail(identifier)
        return {"available": bool(result["items"]), **result}
