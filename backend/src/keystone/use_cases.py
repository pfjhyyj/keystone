"""Explicit cross-domain use cases, assembled by bootstrap."""


class RoleDeletion:
    def __init__(self, identity, files):
        self.identity, self.files = identity, files

    async def execute(self, identifier):
        async with self.identity.context.access(
            "role:delete",
            write=True,
            action="identity.role.delete",
            target="identity.role",
            target_id=identifier,
        ) as (session, auth):
            role, permissions = await self.identity.prepare_role_deletion(session, auth, identifier)
            await self.files.ensure_role_unused(session, identifier)
            await self.identity.delete_role(session, auth, role, permissions)


class FlowCosts:
    """Keep Flow visibility checks around the separately authorized model summary."""

    def __init__(self, flows, models):
        self.flows, self.models = flows, models

    async def execute(self, identifier):
        await self.flows.detail(identifier)
        result = await self.models.summary(flow_run_id=identifier)
        await self.flows.detail(identifier)
        return result
