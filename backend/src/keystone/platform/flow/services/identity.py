from uuid import UUID

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.flow.models import TERMINAL, FlowRun
from keystone.platform.flow.schemas.snapshots import matches_definition, read_snapshot
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import load_authorization


def retained_token_ids():
    """SQL subquery owned by Flow; terminal Runs retain their original PAT too."""
    return select(FlowRun.personal_access_token_id).where(
        FlowRun.personal_access_token_id.is_not(None)
    )


async def retained_execution_ids(database, identifiers):
    parsed = {value: UUID(value) for value in identifiers if _uuid(value)}
    async with database.transaction() as session:
        retained = set(
            await session.scalars(select(FlowRun.id).where(FlowRun.id.in_(parsed.values())))
        )
        return {value for value, identifier in parsed.items() if identifier in retained}


def _uuid(value):
    try:
        UUID(value)
        return True
    except ValueError:
        return False


async def authorize_execution(session, run, definitions, *, execution_version, guard=None):
    definition = definitions.get(run.definition_key)
    if run.execution_version != execution_version or definition is None:
        raise ServiceError(409, "execution_version_unavailable", "原执行版本不可用")
    snapshot = read_snapshot(run.definition_snapshot)
    if not matches_definition(snapshot, definition):
        raise ServiceError(409, "execution_version_unavailable", "运行契约与当前制品不匹配")
    if run.status in TERMINAL or run.cancel_requested_at:
        raise ServiceError(409, "flow_cancelled", "流程已结束或正在取消")
    try:
        auth = await load_authorization(session, run.actor_id, pat_id=run.personal_access_token_id)
        for permission in ("flow:start", *definition.required_permissions):
            auth.require(permission)
    except ServiceError:
        raise ServiceError(403, "execution_not_authorized", "原执行身份或权限已失效") from None
    if guard is not None:
        # An additional business check cannot replace the original user/PAT authority.
        try:
            await guard(session, auth, run.id, run.definition_key, dict(run.input))
        except ServiceError as error:
            if error.status in {401, 403, 404}:
                raise ServiceError(403, "execution_not_authorized", "业务执行资格已失效") from None
            raise  # Dependency outages remain retryable instead of failing a pending Run.
    return auth


class ExecutionIdentity(AccessContext):
    """Trusted Run reference, never a serialized credential or an HTTP authentication mode."""

    def __init__(self, runtime, run_id):
        super().__init__(runtime.database, runtime.settings)
        self.run_id = UUID(str(run_id))
        self.flow_definitions = runtime.flow_definitions
        self.execution_version = runtime.execution_version
        self.guard = getattr(runtime, "execution_guard", None)

    async def authorize(self, session):
        run = await session.scalar(
            select(FlowRun).where(FlowRun.id == self.run_id).with_for_update(read=True)
        )
        if run is None:
            raise ServiceError(404, "flow_not_found", "流程不存在")
        return await authorize_execution(
            session,
            run,
            self.flow_definitions,
            execution_version=self.execution_version(),
            guard=self.guard,
        )
