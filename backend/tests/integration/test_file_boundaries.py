"""Public Identity/Files boundaries preserve live grants, transactions and file effects."""

from uuid import UUID, uuid4

import pytest
from test_files_dictionary import add_grant, upload
from test_files_dictionary import client as client
from test_identity import NEW_PASSWORD, PASSWORD, assign, bearer, create_user, login, role, token

from keystone.core.errors import ServiceError
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.services.files import FileService
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext

pytestmark = pytest.mark.postgres


def test_empty_role_membership_grants_content_without_expanding_pat_scope(client):
    source = upload(client).json()["id"]
    user = create_user(client)
    functional = role(client, "functional", ["file:inspect", "file:content_read"])
    object_role = role(client, "object_only", [])
    for value in (functional, object_role):
        assert assign(client, user["id"], value["id"]).status_code == 204
    for action in ("inspect", "content_read"):
        assert (
            add_grant(
                client, source, action, subject_type="role", role_id=object_role["id"]
            ).status_code
            == 201
        )
    login(client, "alice")
    assert (
        client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    login(client, "alice", NEW_PASSWORD)
    read = bearer(token(client, ["file:content_read"])["token"])
    path = f"/api/v1/files/{source}"
    assert client.get(path).status_code == 200
    assert client.get(path + "/content", headers=read).content == b"hello"
    assert client.get(path, headers=read).status_code == 403
    login(client)
    assert assign(client, user["id"], object_role["id"], False).status_code == 204
    assert client.get(path + "/content", headers=read).status_code == 404
    assert assign(client, user["id"], object_role["id"]).status_code == 204
    assert client.get(path + "/content", headers=read).content == b"hello"
    assert assign(client, user["id"], functional["id"], False).status_code == 204
    assert client.get(path + "/content", headers=read).status_code == 403


def test_subject_checks_preserve_disabled_users_and_grant_audit_atomicity(client, monkeypatch):
    source = upload(client).json()["id"]
    user = create_user(client)
    assert (
        client.put(f"/api/v1/users/{user['id']}/active", json={"is_active": False}).status_code
        == 200
    )
    assert add_grant(client, source, subject_type="user", user_id=user["id"]).status_code == 201
    path = f"/api/v1/files/{source}/grants"
    before = client.get(path).json()
    for subject, identifier in (("user", "user_id"), ("role", "role_id")):
        result = add_grant(client, source, subject_type=subject, **{identifier: str(uuid4())})
        assert result.status_code == 404 and result.json()["code"] == "not_found"
        assert client.get(path).json() == before

    original = audit.record

    async def fail_success(*args, **kwargs):
        if args[1] == "file.grant" and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("audit unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(audit, "record", fail_success)
    assert add_grant(client, source, subject_type="authenticated").status_code == 500
    assert client.get(path).json() == before


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
    assert client.get(f"/api/v1/files/{copied}/references").json() == [
        {"owner": "test:copy", "slot": "output"}
    ]
    assert not list((runtime.settings.file_coordination_root / ".transfers").iterdir())
