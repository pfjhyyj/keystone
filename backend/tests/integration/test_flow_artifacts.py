"""Run artifacts over real SQL/storage, without business publication rules or DBOS jobs."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import delete, update
from support.local_media_stub import definition
from support.storage_support import s3_endpoint
from test_files_dictionary import client as client
from test_files_dictionary import reference_rows, upload
from test_identity import bearer, create_user, query, token

from keystone.core.errors import ServiceError
from keystone.core.storage_config import StorageConfig
from keystone.core.time import utcnow
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services.references import FileReferences
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.services.artifacts import FlowArtifactService
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.flow.workflows import context as host
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext

pytestmark = pytest.mark.postgres


@pytest.fixture
def artifacts(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.flow_definitions = {definition.key: definition}
    monkeypatch.setattr(host, "_bound_runtime", runtime)
    source = upload(client).json()["id"]
    run_id = start(client, source)
    return client, run_id, source


def start(client, source, key="first", headers=None):
    response = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition.key,
            "request_key": key,
            "input": {"source_file_id": source},
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def put(client, run_id, key="report", data=b"result", **kwargs):
    return client.portal.call(
        lambda: host._upload(run_id, key, "result.txt", data, content_type="text/plain", **kwargs)
    )


def service(client, run_id, credential=None):
    runtime = client.app.state.runtime
    identity = (
        AccessContext(runtime.database, runtime.settings, credential)
        if credential
        else ExecutionIdentity(runtime, run_id)
    )
    return FlowArtifactService(identity, runtime.storage)


def require(client, svc, run_id, expected, *, adopt=False, rollback=False):
    async def operation():
        async with svc.identity.access() as (session, auth):
            await svc.require_in_transaction(session, auth, run_id, expected)
            if adopt:
                await FileReferences(session, auth, "adoption").replace(
                    "record", "chosen", expected.values()
                )
            if rollback:
                raise RuntimeError("business failed")

    return client.portal.call(operation)


def test_find_verified_id_missing_key_and_upload_replay_without_new_references(artifacts):
    client, run_id, source = artifacts
    assert client.portal.call(host._artifact, run_id, "report") is None
    assert client.portal.call(host._artifact, run_id, "input") is None
    identifier = put(client, run_id)
    assert put(client, run_id) == identifier
    assert client.portal.call(host._artifact, run_id, "report") == identifier
    assert client.portal.call(host._artifact, run_id, "other") is None
    assert {"owner": f"flow:{run_id}", "slot": "input"} in reference_rows(client, source)
    assert reference_rows(client, identifier) == [
        {"owner": f"flow:{run_id}", "slot": "artifact.report"}
    ]


def test_execution_cannot_query_another_run_even_with_the_same_actor(artifacts):
    client, run_id, source = artifacts
    other = start(client, source, "second")
    identifier = put(client, other)
    svc = service(client, run_id)
    with pytest.raises(ServiceError) as rejected:
        client.portal.call(svc.find, other, "report")
    assert rejected.value.code == "flow_not_found"
    with pytest.raises(ServiceError) as rejected:
        require(client, svc, run_id, {"report": identifier})
    assert rejected.value.code == "flow_invalid_artifact"
    assert client.portal.call(svc.find, run_id, "report") is None


def test_read_only_pat_needs_no_file_management_or_inspection_permission(artifacts):
    client, run_id, _ = artifacts
    identifier = put(client, run_id)
    secret = token(client, ["flow:read", "test_files.asset:content_read"])["token"]
    svc = service(client, run_id, Credential("pat", secret))
    assert client.portal.call(svc.find, run_id, "report") == identifier
    require(client, svc, run_id, {"report": identifier})


def test_run_visibility_and_file_permission_are_independent(artifacts):
    client, run_id, _ = artifacts
    identifier = put(client, run_id)
    other = create_user(client)
    query(
        client,
        lambda session: session.execute(
            update(FlowRun).where(FlowRun.id == UUID(run_id)).values(actor_id=UUID(other["id"]))
        ),
    )
    secret = token(client, ["flow:read", "test_files.asset:content_read"])["token"]
    with pytest.raises(ServiceError) as rejected:
        client.portal.call(
            service(client, run_id, Credential("pat", secret)).find, run_id, "report"
        )
    assert rejected.value.code == "flow_not_found"
    secret = token(client, ["flow:read", "flow:manage", "test_files.asset:content_read"])["token"]
    svc = service(client, run_id, Credential("pat", secret))
    assert client.portal.call(svc.find, run_id, "report") == identifier
    # A monitoring permission does not authorize an unrelated business download.
    assert client.get(f"/api/v1/_test/materials/{identifier}/content").status_code == 404


@pytest.mark.parametrize("permissions", [["test_files.asset:content_read"]])
def test_missing_functional_permissions_cannot_probe_even_a_missing_key(artifacts, permissions):
    client, run_id, _ = artifacts
    secret = token(client, permissions)["token"]
    with pytest.raises(ServiceError) as rejected:
        client.portal.call(
            service(client, run_id, Credential("pat", secret)).find, run_id, "missing"
        )
    assert rejected.value.status == 403


def test_historical_read_does_not_resume_a_terminal_or_old_execution(artifacts):
    client, run_id, _ = artifacts
    identifier = put(client, run_id)
    query(
        client,
        lambda session: session.execute(
            update(FlowRun)
            .where(FlowRun.id == UUID(run_id))
            .values(status="failed", finished_at=utcnow(), execution_version="0" * 64)
        ),
    )
    secret = token(client, ["flow:read", "test_files.asset:content_read"])["token"]
    assert (
        client.portal.call(
            service(client, run_id, Credential("pat", secret)).find, run_id, "report"
        )
        == identifier
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ("disabled", "file_unavailable"),
        ("corrupt", "file_corrupt"),
        ("size", "storage_unavailable"),
        ("missing", "storage_unavailable"),
        ("marker", "storage_unavailable"),
        ("limit", "file_too_large"),
    ],
)
def test_find_never_treats_an_unusable_registered_file_as_absent(artifacts, change, code):
    client, run_id, _ = artifacts
    identifier = put(client, run_id)
    assert client.portal.call(host._artifact, run_id, "report") == identifier
    rt = client.app.state.runtime
    path = rt.storage.get().root / "files" / UUID(identifier).hex
    if change == "disabled":
        query(
            client,
            lambda session: session.execute(
                update(File).where(File.id == UUID(identifier)).values(status="disabled")
            ),
        )
    elif change == "corrupt":
        path.write_bytes(b"broken")
    elif change == "size":
        path.write_bytes(b"x")
    elif change == "missing":
        path.unlink()
    elif change == "marker":
        (rt.storage.get().root / ".keystone-storage").write_text("invalid", encoding="utf-8")
    with pytest.raises(ServiceError) as rejected:
        client.portal.call(
            lambda: host._artifact(run_id, "report", max_bytes=1 if change == "limit" else None)
        )
    assert rejected.value.code == code


def test_ambiguous_logical_keys_fail_closed_in_both_interfaces(artifacts):
    client, run_id, _ = artifacts
    identifier, other = put(client, run_id), put(client, run_id, "other")

    async def duplicate(session):
        session.add(
            FileReference(owner=f"flow:{run_id}", slot="artifact.report", file_id=UUID(other))
        )

    query(client, duplicate)
    svc = service(client, run_id)
    for operation in [
        lambda: client.portal.call(svc.find, run_id, "report"),
        lambda: require(client, svc, run_id, {"report": identifier}),
    ]:
        with pytest.raises(ServiceError) as rejected:
            operation()
        assert rejected.value.code == "flow_artifact_ambiguous"


def test_claims_do_not_accept_input_references_or_an_unrelated_file(artifacts):
    client, run_id, source = artifacts
    svc = service(client, run_id)
    for expected in [{"input": source}, {"result": source}]:
        with pytest.raises(ServiceError) as rejected:
            require(client, svc, run_id, expected)
        assert rejected.value.code == "flow_invalid_artifact"


def test_transaction_check_does_no_storage_io_and_never_commits_business_adoption(
    artifacts, monkeypatch
):
    client, run_id, _ = artifacts
    identifier = put(client, run_id)
    svc = service(client, run_id)

    def unexpected(*args, **kwargs):
        pytest.fail("Byte verification inside the business transaction")

    monkeypatch.setattr(client.app.state.runtime.storage.get(), "open_local", unexpected)
    require(client, svc, run_id, {"report": identifier})
    with pytest.raises(RuntimeError, match="business failed"):
        require(client, svc, run_id, {"report": identifier}, adopt=True, rollback=True)
    refs = reference_rows(client, identifier)
    assert refs == [{"owner": f"flow:{run_id}", "slot": "artifact.report"}]
    require(client, svc, run_id, {"report": identifier}, adopt=True)
    assert {r["owner"] for r in reference_rows(client, identifier)} == {
        f"flow:{run_id}",
        "adoption:record",
    }


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("reference", "flow_invalid_artifact"),
        ("cancel", "flow_cancelled"),
    ],
)
def test_rechecks_after_io_without_holding_a_run_or_identity_transaction(
    artifacts, monkeypatch, mutation, code
):
    client, run_id, _ = artifacts
    put(client, run_id)
    source = client.app.state.runtime.storage.get()
    opened, release = Event(), Event()
    original = source.open_local

    def pause(*args):
        stream = original(*args)
        opened.set()
        try:
            assert release.wait(10)
        except BaseException:
            stream.close()
            raise
        return stream

    monkeypatch.setattr(source, "open_local", pause)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.portal.call, host._artifact, run_id, "report")
        try:
            assert opened.wait(5)
            if mutation == "reference":
                query(
                    client,
                    lambda session: session.execute(
                        delete(FileReference).where(
                            FileReference.owner == f"flow:{run_id}",
                            FileReference.slot == "artifact.report",
                        )
                    ),
                )
            else:
                assert client.post(f"/api/v1/flow-runs/{run_id}/cancel").status_code == 200
        finally:
            release.set()
        with pytest.raises(ServiceError) as rejected:
            pending.result(timeout=10)
        assert rejected.value.code == code


def test_transaction_entry_cannot_widen_a_revoked_execution_pat_using_session_auth(artifacts):
    client, _, source = artifacts
    issued = token(
        client, ["flow:start", "test_files.asset:create", "test_files.asset:content_read"]
    )
    run_id = start(client, source, "pat", bearer(issued["token"]))
    identifier = put(client, run_id)
    assert client.delete(f"/api/v1/auth/tokens/{issued['id']}").status_code == 204
    svc = service(client, run_id)
    normal = AccessContext(
        client.app.state.runtime.database,
        client.app.state.runtime.settings,
        Credential("session", client.cookies.get("keystone_session")),
    )

    async def operation():
        async with normal.access() as (session, auth):
            await svc.require_in_transaction(session, auth, run_id, {"report": identifier})

    with pytest.raises(ServiceError) as rejected:
        client.portal.call(operation)
    assert rejected.value.code == "execution_not_authorized"


def test_s3_artifact_queries_use_existing_bounded_integrity_checks(artifacts):
    client, run_id, _ = artifacts
    rt = client.app.state.runtime
    with s3_endpoint() as state:
        rt.settings.file_storages["cloud"] = StorageConfig(
            driver="s3",
            endpoint=state.endpoint,
            bucket="artifacts",
            access_key="test-access",
            secret_key="test-secret",
        )
        rt.storage = StorageRegistry(rt.settings)
        identifier = put(client, run_id, storage_key="cloud")
        assert client.portal.call(host._artifact, run_id, "report") == identifier
        key = next(iter(state.objects))
        state.objects[key] = b"broken"
        with pytest.raises(ServiceError) as rejected:
            client.portal.call(host._artifact, run_id, "report")
        assert rejected.value.code == "file_corrupt"


def test_maximum_key_and_shared_file_do_not_add_business_distinctness_rules(artifacts):
    client, run_id, _ = artifacts
    key = "x" * 86
    identifier = put(client, run_id, key)
    assert put(client, run_id, key) == identifier
    assert client.portal.call(host._artifact, run_id, key) == identifier
    svc = service(client, run_id)

    async def alias():
        async with svc.identity.access() as (session, auth):
            await FileReferences(session, auth, "flow").replace(
                run_id, "artifact.alias", [identifier]
            )

    client.portal.call(alias)
    require(client, svc, run_id, {key: identifier, "alias": identifier})


def test_transaction_rechecks_current_file_access_before_adoption(artifacts):
    client, run_id, _ = artifacts
    identifier = put(client, run_id)
    query(
        client,
        lambda session: session.execute(
            update(File).where(File.id == UUID(identifier)).values(status="disabled")
        ),
    )
    with pytest.raises(ServiceError) as rejected:
        require(client, service(client, run_id), run_id, {"report": identifier}, adopt=True)
    assert rejected.value.code == "file_unavailable"
    assert reference_rows(client, identifier) == [
        {"owner": f"flow:{run_id}", "slot": "artifact.report"}
    ]


def test_public_context_limits_files_to_run_and_returns_input_copies(artifacts):
    client, run_id, source = artifacts
    ctx = host.flow_context(run_id)
    assert client.portal.call(lambda: ctx.read_file(source, max_bytes=64)) == b"hello"
    inputs = client.portal.call(ctx.inputs)
    inputs["source_file_id"] = "changed"
    assert client.portal.call(ctx.inputs)["source_file_id"] == source
    own = put(client, run_id)
    assert client.portal.call(lambda: ctx.read_file(own, max_bytes=64)) == b"result"
    unrelated = upload(client, key="outside-context").json()["id"]
    other = put(client, start(client, source, key="other"))

    async def materialize(identifier):
        async with ctx.materialize_file(identifier):
            pytest.fail("Unregistered content must not be materialized")

    for identifier in (unrelated, other):
        for operation in (
            lambda identifier=identifier: ctx.read_file(identifier, max_bytes=64),
            lambda identifier=identifier: materialize(identifier),
        ):
            with pytest.raises(ServiceError) as error:
                client.portal.call(operation)
            assert error.value.code == "flow_file_scope_denied"
