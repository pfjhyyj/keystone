"""Public Identity/Files boundaries preserve live grants, transactions and file effects."""

from uuid import UUID

import pytest
from test_files_dictionary import client as client
from test_files_dictionary import reference_rows, upload

from keystone.core.errors import ServiceError
from keystone.platform.files.services.files import FileService
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext

pytestmark = pytest.mark.postgres


def test_public_file_transfer_owns_workspace_and_preserves_referenced_upload_idempotency(client):
    source = UUID(upload(client).json()["id"])
    runtime = client.app.state.runtime
    service = FileService(
        AccessContext(
            runtime.database,
            runtime.settings,
            Credential("session", client.cookies["keystone_session"]),
        ),
        runtime.storage,
    )

    async def exercise():
        options = dict(
            name="copy.txt",
            content_type="text/plain",
            request_key="boundary:copy",
            owner="test:copy",
            slot="output",
        )
        with service.transfer() as reader:
            async with reader.materialize_file(source) as path:
                with pytest.raises(ServiceError) as busy:
                    await reader.upload_file(path, **options)
                assert busy.value.code == "file_busy" and path.read_bytes() == b"hello"
                with service.transfer() as writer:
                    first = await writer.upload_file(path, **options)
                    assert (await writer.upload_file(path, **options))["id"] == first["id"]
                assert path.read_bytes() == b"hello"
            assert not path.exists()
        with pytest.raises(RuntimeError, match="scope is closed"):
            async with reader.materialize_file(source):
                pytest.fail("Closed transfer reused a released slot")
        assert await service.content(first["id"]) == ("copy.txt", b"hello")
        return str(first["id"])

    copied = client.portal.call(exercise)
    assert reference_rows(client, copied) == [{"owner": "test:copy", "slot": "output"}]
    assert not list((runtime.settings.file_coordination_root / ".transfers").iterdir())
