from uuid import UUID

from sqlalchemy import select

from keystone.composition import execution_version
from keystone.core.errors import ServiceError
from keystone.platform.flow.models import TERMINAL, FlowRun
from keystone.platform.identity.access import AccessContext
from keystone.platform.identity.policy import load_authorization


async def authorize_execution(session, run, definitions):
    definition = definitions.get(run.definition_key)
    if run.execution_version != execution_version() or definition is None:
        raise ServiceError(409, "execution_version_unavailable", "原执行版本不可用")
    if run.status in TERMINAL or run.cancel_requested_at:
        raise ServiceError(409, "flow_cancelled", "流程已结束或正在取消")
    try:
        auth = await load_authorization(session, run.actor_id, pat_id=run.personal_access_token_id)
        for permission in ("flow:start", *definition.required_permissions):
            auth.require(permission)
        return auth
    except ServiceError:
        raise ServiceError(403, "execution_not_authorized", "原执行身份或权限已失效") from None


class ExecutionIdentity(AccessContext):
    """Trusted Run reference, never a serialized credential or an HTTP authentication mode."""

    def __init__(self, runtime, run_id):
        super().__init__(runtime.database, runtime.settings)
        self.run_id = UUID(str(run_id))
        self.flow_definitions = runtime.flow_definitions

    async def authorize(self, session):
        run = await session.scalar(
            select(FlowRun).where(FlowRun.id == self.run_id).with_for_update(read=True)
        )
        if run is None:
            raise ServiceError(404, "flow_not_found", "流程不存在")
        return await authorize_execution(session, run, self.flow_definitions)
