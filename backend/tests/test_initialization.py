import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from keystone.bootstrap import create_app
from keystone.composition import build_catalog
from keystone.core.asyncio import new_event_loop, run
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.entrypoints.cli import initialize
from keystone.platform.audit.models import AuditLog
from keystone.platform.identity.models import User
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation

pytestmark = pytest.mark.postgres
PASSWORD = "initial-admin-test-password"


def configuration(empty_database_url, tmp_path):
    return Settings(
        _env_file=None,
        env="test",
        database_url=empty_database_url,
        file_coordination_root=tmp_path,
        file_storages={"local": {"driver": "filesystem", "root": str(tmp_path)}},
        initial_admin_password=PASSWORD,
        model_secrets={},
        browser_origins=["http://127.0.0.1:5173"],
    )


def test_init_prepares_all_dependencies_and_preserves_existing_accounts(
    empty_database_url, tmp_path
):
    settings = configuration(empty_database_url, tmp_path)
    initialize(settings, non_interactive=True)
    marker = (tmp_path / ".keystone-storage").read_bytes()
    # Changing/removing the bootstrap password must never reset an existing account.
    settings.initial_admin_password = SecretStr("different-bootstrap-password")
    initialize(settings, non_interactive=True)
    settings.initial_admin_password = None
    initialize(settings, non_interactive=True)
    assert (tmp_path / ".keystone-storage").read_bytes() == marker

    with TestClient(
        create_app(settings), backend_options={"loop_factory": new_event_loop}
    ) as client:
        assert client.get("/api/v1/ready").status_code == 200
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": PASSWORD},
                headers={"Origin": "http://127.0.0.1:5173", "X-Keystone-Request": "1"},
            ).status_code
            == 200
        )

    async def counts():
        database = Database(settings, catalog=build_catalog())
        try:
            async with database.transaction() as session:
                assert await session.scalar(select(func.count()).select_from(User)) == 1
                for model in (Channel, ModelEntry, ModelInvocation):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(AuditLog.action == "identity.admin.initialize")
                    )
                    == 1
                )
                from sqlalchemy import text

                assert await session.scalar(text("SELECT to_regclass('dbos.workflow_status')"))
        finally:
            await database.close()

    run(counts())


def test_init_requires_a_password_only_for_an_empty_installation(empty_database_url, tmp_path):
    settings = configuration(empty_database_url, tmp_path)
    settings.initial_admin_password = None
    with pytest.raises(ServiceError) as missing:
        initialize(settings, non_interactive=True)
    assert missing.value.code == "initial_admin_password_required"
    settings.initial_admin_password = SecretStr(PASSWORD)
    initialize(settings, non_interactive=True)
