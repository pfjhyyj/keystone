"""Default model per Flow role, used when a start request leaves that role unbound."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.models import FlowModelDefault
from keystone.platform.model_access.services.entries import (
    describe_binding_in_transaction,
    entry_keys_in_transaction,
    resolve_bindings_in_transaction,
)

CONFIGURE = "flow:configure"
MAX_MODELS = 200


def _role(definitions, definition_key, role_key):
    definition = definitions.get(definition_key)
    roles = definition.model_roles if definition else ()
    role = next((role for role in roles if role.key == role_key), None)
    if role is None:
        raise ServiceError(404, "flow_model_role_not_found", "模型用途不存在")
    return definition, role


async def fill_bindings_in_transaction(session, definition, bindings):
    """Bind roles the request left out to their defaults; explicit bindings win."""
    missing = [role.key for role in definition.model_roles if role.key not in bindings]
    if not missing:
        return dict(bindings)
    rows = (
        await session.execute(
            select(FlowModelDefault.role, FlowModelDefault.model_entry_id).where(
                FlowModelDefault.definition_key == definition.key,
                FlowModelDefault.role.in_(missing),
            )
        )
    ).all()
    keys = await entry_keys_in_transaction(session, [entry for _, entry in rows])
    defaults = {role: keys[entry] for role, entry in rows if entry in keys}
    if set(missing) - defaults.keys():
        raise ServiceError(409, "flow_model_unset", "管理员尚未为该模型用途选择模型")
    return {**bindings, **defaults}


class FlowModelDefaults:
    def __init__(self, identity, definitions, models):
        # models: ModelEntryService for the same identity; it lists the usable models.
        self.identity, self.definitions, self.models = identity, definitions, models

    async def listing(self):
        """Every role of the assembled definitions with its current default model key."""
        async with self.identity.access(CONFIGURE) as (session, _):
            rows = list(
                await session.scalars(
                    select(FlowModelDefault).where(
                        FlowModelDefault.definition_key.in_(self.definitions.keys())
                    )
                )
            )
            keys = await entry_keys_in_transaction(session, [row.model_entry_id for row in rows])
            current = {(row.definition_key, row.role): keys.get(row.model_entry_id) for row in rows}
            return [
                {
                    "definition_key": definition.key,
                    "definition_name": definition.name,
                    "role": role.key,
                    "role_label": role.label,
                    "operation": role.operation,
                    "delivery": role.delivery,
                    "model_key": current.get((definition.key, role.key)),
                }
                for definition in sorted(self.definitions.values(), key=lambda d: d.key)
                for role in definition.model_roles
            ]

    async def current(self, definition_key, role_key, *, definition_revision=None):
        async with self.identity.access() as (session, auth):
            return await self.current_in_transaction(
                session, auth, definition_key, role_key, definition_revision=definition_revision
            )

    async def current_in_transaction(
        self, session, auth, definition_key, role_key, *, definition_revision=None
    ):
        """The role's current default model and the output options its protocol declares.

        Same functional checks as a start that leaves the role unbound: flow:start, the
        definition's permissions and model:invoke. Reads without locks or audit.
        """
        auth.require("flow:start")
        definition = self.definitions.get(definition_key)
        if definition is None:
            raise ServiceError(404, "definition_unavailable", "流程定义不可用")
        if definition_revision is not None and definition_revision != definition.revision:
            raise ServiceError(409, "flow_revision_conflict", "流程契约版本已改变，请刷新目录")
        for permission in definition.required_permissions:
            auth.require(permission)
        _, role = _role(self.definitions, definition_key, role_key)
        auth.require("model:invoke")
        entry_id = await session.scalar(
            select(FlowModelDefault.model_entry_id).where(
                FlowModelDefault.definition_key == definition.key,
                FlowModelDefault.role == role.key,
            )
        )
        model = (
            await describe_binding_in_transaction(
                session, entry_id, operation=role.operation, delivery=role.delivery
            )
            if entry_id is not None
            else None
        )
        if model is None:
            status, code = "unset", "flow_model_unset"
        else:
            code = model["unavailable_reason"]
            status = "available" if code is None else "unavailable"
        return {
            "definition_key": definition.key,
            "definition_revision": definition.revision,
            "role": role.key,
            "operation": role.operation,
            "delivery": role.delivery,
            "status": status,
            # The code a start leaving this role unbound would currently return.
            "error_code": code,
            "model": model and {"key": model["key"], "name": model["name"]},
            "output_options": model and model["output_options"],
        }

    async def choices(self, definition_key, role_key):
        """Active models that can serve the role; listing them also needs model:invoke."""
        _, role = _role(self.definitions, definition_key, role_key)
        async with self.identity.access(CONFIGURE):
            pass
        catalog = await self.models.catalog(
            0, MAX_MODELS, operation=role.operation, delivery=role.delivery
        )
        return [{"key": item["key"], "name": item["name"]} for item in catalog["items"]]

    async def save(self, definition_key, role_key, model_key):
        definition, role = _role(self.definitions, definition_key, role_key)
        async with self.identity.access(
            CONFIGURE, action="flow.model_default.save", target="flow.model_default"
        ) as (session, auth):
            # Same checks as a start; locks Entry then Channel before the default row.
            resolved = await resolve_bindings_in_transaction(
                session, auth, roles=(role,), bindings={role.key: model_key}
            )
            entry_id = UUID(str(resolved[role.key]["selection"]["model_entry_id"]))
            inserted = await session.scalar(
                insert(FlowModelDefault)
                .values(
                    definition_key=definition.key,
                    role=role.key,
                    model_entry_id=entry_id,
                    updated_by=auth.user.id,
                )
                .on_conflict_do_nothing()
                .returning(FlowModelDefault.role)
            )
            before = None
            if inserted is None:
                row = await session.get(
                    FlowModelDefault,
                    (definition.key, role.key),
                    with_for_update=True,
                    populate_existing=True,
                )
                if row is None:
                    # The previous default's entry was deleted after the insert saw it.
                    raise ServiceError(
                        409, "flow_model_default_changed", "默认模型刚被修改，请重试"
                    )
                before = (await entry_keys_in_transaction(session, [row.model_entry_id])).get(
                    row.model_entry_id
                )
                row.model_entry_id, row.updated_by = entry_id, auth.user.id
                row.updated_at = utcnow()
                await session.flush()
            await self.identity.log(
                session,
                auth,
                "flow.model_default.save",
                "flow.model_default",
                None,
                {
                    "definition_key": definition.key,
                    "role": role.key,
                    "before": before,
                    "after": model_key,
                },
            )
            return {"definition_key": definition.key, "role": role.key, "model_key": model_key}
