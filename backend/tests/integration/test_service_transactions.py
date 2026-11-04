"""A test-only business composes public Services in one identity transaction."""

import asyncio
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from support.flow_fixture.contracts import PACKAGE
from support.flow_packages import install_package
from support.model_stub import install as install_models
from test_business_integration import ACTION, WRITE, install
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import wait_run, worker
from test_identity import NEW_PASSWORD, PASSWORD, assign, create_user, login, query, role, token
from test_model_access import channel, entry

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferences
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.schemas import Start
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.roles import RoleService
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres
PERMISSIONS = [
    WRITE,
    "test_files.asset:inspect",
    "test_files.asset:manage",
    "test_files.asset:create",
    "test_files.asset:content_read",
    "flow:start",
    "flow:read",
    "flow:cancel",
    "model:invoke",
]


@pytest.fixture
def case(monkeypatch, request):
    install(monkeypatch)
    browser = request.getfixturevalue("client")
    install_models(monkeypatch)
    install_package(browser, monkeypatch, PACKAGE)
    entry(browser, channel(browser))
    recipient = create_user(browser)
    source = upload(browser).json()
    query(
        browser,
        lambda session: session.execute(
            text("""CREATE TABLE keystone.test_notes_transactions (
        actor_id uuid NOT NULL, request_key text NOT NULL,
        run_id uuid NOT NULL REFERENCES keystone.flow_runs(id),
        file_id uuid NOT NULL, recipient_id uuid NOT NULL, status text NOT NULL,
        PRIMARY KEY (actor_id,request_key))""")
        ),
    )
    return browser, UUID(source["id"]), UUID(recipient["id"])


def context(browser, secret=None):
    rt = browser.app.state.runtime
    credential = (
        Credential("pat", secret)
        if secret
        else Credential("session", browser.cookies.get("keystone_session"))
    )
    return AccessContext(rt.database, rt.settings, credential, correlation_id="test-composed-write")


def start_body(identifier, key):
    return Start(
        definition_key="document.review",
        request_key="test-notes:" + key,
        input={"source_file_id": str(identifier)},
        model_bindings={"review": "chat"},
    )


async def save(browser, identity, identifier, recipient, key, *, after_write=None):
    rt = browser.app.state.runtime
    files, flows = FileService(identity, rt.storage), rt.flow_service(identity)
    async with identity.access(WRITE, action=ACTION, target="test_notes.note", target_id=key) as (
        session,
        auth,
    ):
        old = (
            await session.execute(
                text("""SELECT run_id,file_id,recipient_id FROM keystone.test_notes_transactions
            WHERE actor_id=:actor AND request_key=:key"""),
                {"actor": auth.user.id, "key": key},
            )
        ).first()
        if old:
            if (old.file_id, old.recipient_id) != (identifier, recipient):
                raise ServiceError(409, "idempotency_conflict", "Test request changed")
            return await flows.get_in_transaction(session, auth, old.run_id)
        metadata = await files.metadata_in_transaction(session, identifier)
        assert (
            metadata["id"] == identifier
            and not {"path", "object_key", "storage_fingerprint"} & metadata.keys()
        )
        run = await flows.start_in_transaction(session, auth, start_body(identifier, key))
        # Business references share this session and authorization.
        await FileReferences(session, auth, "test_notes").replace(key, "attachment", [identifier])
        await session.execute(
            text("""INSERT INTO keystone.test_notes_transactions
            (actor_id,request_key,run_id,file_id,recipient_id,status)
            VALUES (:actor,:key,:run,:file,:recipient,'pending')"""),
            {
                "actor": auth.user.id,
                "key": key,
                "run": run["id"],
                "file": identifier,
                "recipient": recipient,
            },
        )
        await identity.log(session, auth, ACTION, "test_notes.note", key, {"status": "pending"})
        if after_write:
            await after_write(session)
        return run


async def cancel(browser, identity, key, *, after_write=None):
    flows = browser.app.state.runtime.flow_service(identity)
    async with identity.access(WRITE, action=ACTION, target="test_notes.note", target_id=key) as (
        session,
        auth,
    ):
        old = (
            await session.execute(
                text("""SELECT run_id,status FROM keystone.test_notes_transactions
            WHERE actor_id=:actor AND request_key=:key FOR UPDATE"""),
                {"actor": auth.user.id, "key": key},
            )
        ).one()
        run = await flows.cancel_in_transaction(session, auth, old.run_id)
        if old.status != "cancel_requested":
            await session.execute(
                text("""UPDATE keystone.test_notes_transactions SET status='cancel_requested'
                WHERE actor_id=:actor AND request_key=:key"""),
                {"actor": auth.user.id, "key": key},
            )
            await identity.log(
                session, auth, ACTION, "test_notes.note", key, {"status": "cancel_requested"}
            )
        if after_write:
            await after_write(session)
        return run


def facts(browser):
    async def read(session):
        return (
            list(
                (
                    await session.execute(
                        text(
                            "SELECT actor_id,request_key,run_id,status "
                            "FROM keystone.test_notes_transactions ORDER BY request_key"
                        )
                    )
                ).all()
            ),
            list(
                (
                    await session.execute(
                        select(FlowRun.id, FlowRun.status, FlowRun.cancel_requested_at).order_by(
                            FlowRun.id
                        )
                    )
                ).all()
            ),
            list(
                (
                    await session.execute(
                        select(
                            FileReference.owner, FileReference.slot, FileReference.file_id
                        ).order_by(FileReference.owner, FileReference.slot, FileReference.file_id)
                    )
                ).all()
            ),
            list(
                (
                    await session.execute(
                        select(AuditLog.id)
                        .where(
                            AuditLog.outcome == "success",
                            AuditLog.action.in_(
                                (
                                    ACTION,
                                    "flow.start",
                                    "flow.cancel",
                                    "file.reference",
                                )
                            ),
                        )
                        .order_by(AuditLog.id)
                    )
                ).scalars()
            ),
        )

    return query(browser, read)


def test_composed_save_rolls_back_audit_failure_and_missing_business_scope(case, monkeypatch):
    browser, identifier, recipient = case
    full = context(browser, token(browser, PERMISSIONS)["token"])
    before = facts(browser)
    record = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == ACTION and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("isolated final business audit failure")
        return await record(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError, match="final business audit"):
        browser.portal.call(save, browser, full, identifier, recipient, str(uuid4()))
    assert facts(browser) == before
    monkeypatch.setattr(audit, "record", record)
    limited = context(browser, token(browser, [p for p in PERMISSIONS if p != WRITE])["token"])
    with pytest.raises(ServiceError) as denied:
        browser.portal.call(save, browser, limited, identifier, recipient, str(uuid4()))
    assert denied.value.status == 403 and facts(browser) == before


def test_cancelled_transaction_does_not_publish_pending_work(case):
    browser, identifier, recipient = case
    identity = context(browser)
    before = facts(browser)

    async def interrupted():
        written, release = asyncio.Event(), asyncio.Event()

        async def gate(_session):
            written.set()
            await release.wait()

        task = asyncio.create_task(
            save(browser, identity, identifier, recipient, str(uuid4()), after_write=gate)
        )
        await asyncio.wait_for(written.wait(), 5)
        # A separate connection sees no business row or Run before commit.
        async with browser.app.state.runtime.database.transaction() as session:
            assert await session.scalar(select(FlowRun.id)) is None
            assert (
                await session.scalar(text("SELECT run_id FROM keystone.test_notes_transactions"))
                is None
            )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    browser.portal.call(interrupted)
    assert facts(browser) == before


def test_lost_receipt_reuses_committed_business_run_and_native_worker(case, tmp_path):
    browser, identifier, recipient = case
    identity = context(browser, token(browser, PERMISSIONS)["token"])
    key = str(uuid4())
    run = browser.portal.call(save, browser, identity, identifier, recipient, key)
    committed = facts(browser)
    assert (
        browser.portal.call(save, browser, identity, identifier, recipient, key)["id"] == run["id"]
    )
    assert facts(browser) == committed

    async def replay():
        async with identity.access() as (session, auth):
            return await browser.app.state.runtime.flow_service(identity).start_in_transaction(
                session, auth, start_body(identifier, key)
            )

    assert browser.portal.call(replay)["id"] == run["id"]
    assert facts(browser) == committed
    with pytest.raises(ServiceError) as conflict:
        browser.portal.call(save, browser, identity, identifier, uuid4(), key)
    assert conflict.value.code == "idempotency_conflict" and facts(browser) == committed
    with worker(browser, tmp_path) as (process, log):
        done = wait_run(browser, str(run["id"]), process, log)
        assert done["status"] == "succeeded", (done, log.read_text(encoding="utf-8"))
    invocations = query(browser, lambda session: session.execute(select(ModelInvocation.id)))
    calls = list(invocations)
    assert calls
    assert (
        browser.portal.call(save, browser, identity, identifier, recipient, key)["status"]
        == "succeeded"
    )
    assert (
        list(query(browser, lambda session: session.execute(select(ModelInvocation.id)))) == calls
    )
    assert len(query(browser, lambda session: session.execute(select(FlowRun.id))).all()) == 1
    completed = facts(browser)

    async def cancel_completed():
        async with identity.access() as (session, auth):
            return await browser.app.state.runtime.flow_service(identity).cancel_in_transaction(
                session, auth, run["id"]
            )

    terminal = browser.portal.call(cancel_completed)
    assert terminal["status"] == "succeeded" and terminal["cancel_requested_at"] is None
    assert facts(browser) == completed


def test_cancellation_intent_and_business_fact_commit_or_rollback_together(case, tmp_path):
    browser, identifier, recipient = case
    identity = context(browser)
    key = str(uuid4())
    run = browser.portal.call(save, browser, identity, identifier, recipient, key)
    before = facts(browser)
    limited = context(browser, token(browser, [WRITE, "flow:read"])["token"])
    with pytest.raises(ServiceError) as denied:
        browser.portal.call(cancel, browser, limited, key)
    assert denied.value.status == 403 and facts(browser) == before

    async def reject(_session):
        raise RuntimeError("business cancellation failed")

    with pytest.raises(RuntimeError, match="cancellation failed"):
        browser.portal.call(partial(cancel, browser, identity, key, after_write=reject))
    assert facts(browser) == before
    receipt = browser.portal.call(cancel, browser, identity, key)
    assert receipt["cancel_requested_at"] and receipt["status"] == "pending"
    committed = facts(browser)
    assert (
        browser.portal.call(cancel, browser, identity, key)["cancel_requested_at"]
        == receipt["cancel_requested_at"]
    )
    assert facts(browser) == committed
    with worker(browser, tmp_path) as (process, log):
        done = wait_run(browser, str(run["id"]), process, log)
        assert done["status"] == "cancelled", (done, log.read_text(encoding="utf-8"))
    assert query(browser, lambda session: session.scalar(select(ModelInvocation.id))) is None


def test_transaction_reads_preserve_object_actor_and_current_role_scope(case):
    browser, identifier, recipient = case
    administrator = context(browser)
    key = str(uuid4())
    run = browser.portal.call(save, browser, administrator, identifier, recipient, key)
    viewer = role(
        browser,
        "transaction-viewer",
        [WRITE, "test_files.asset:inspect", "test_files.asset:manage", "flow:read", "flow:cancel"],
    )
    assign(browser, str(recipient), viewer["id"])
    login(browser, "alice")
    assert (
        browser.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    login(browser, "alice", NEW_PASSWORD)
    identity = context(
        browser,
        token(
            browser,
            [
                WRITE,
                "test_files.asset:inspect",
                "test_files.asset:manage",
                "flow:read",
                "flow:cancel",
            ],
        )["token"],
    )
    rt = browser.app.state.runtime

    async def inspect(*, manage=False, flow_action=None):
        async with identity.access(WRITE) as (session, auth):
            if flow_action:
                flows = rt.flow_service(identity)
                return await getattr(flows, flow_action)(session, auth, UUID(str(run["id"])))
            owner = await session.scalar(
                text("""SELECT actor_id FROM keystone.test_notes_transactions
                WHERE file_id=:file AND (actor_id=:actor OR recipient_id=:actor)"""),
                {"file": identifier, "actor": auth.user.id},
            )
            if owner is None or (manage and owner != auth.user.id):
                raise ServiceError(404, "note_not_found", "Test business record is unavailable")
            return await FileService(identity, rt.storage).metadata_in_transaction(
                session, identifier, lock=True
            )

    assert browser.portal.call(inspect)["id"] == identifier
    with pytest.raises(ServiceError) as denied:
        browser.portal.call(partial(inspect, manage=True))
    assert denied.value.code == "note_not_found"
    for action in ("get_in_transaction", "cancel_in_transaction"):
        with pytest.raises(ServiceError) as denied:
            browser.portal.call(partial(inspect, flow_action=action))
        assert denied.value.code == "flow_not_found"
    assert facts(browser)[1][0][2] is None
    # An issued PAT remains the intersection of its scope and current role rights.
    browser.portal.call(
        RoleService(administrator).assign_role, recipient, UUID(viewer["id"]), False
    )
    with pytest.raises(ServiceError) as revoked:
        browser.portal.call(inspect)
    assert revoked.value.status == 403
    for action in ("get_in_transaction", "cancel_in_transaction"):
        with pytest.raises(ServiceError) as revoked:
            browser.portal.call(partial(inspect, flow_action=action))
        assert revoked.value.status == 403


def test_composed_reads_and_cancel_refresh_previously_loaded_provider_rows(case):
    browser, identifier, recipient = case
    identity = context(browser)
    first = browser.portal.call(save, browser, identity, identifier, recipient, str(uuid4()))
    second = browser.portal.call(save, browser, identity, identifier, recipient, str(uuid4()))
    third = browser.portal.call(save, browser, identity, identifier, recipient, str(uuid4()))
    rt = browser.app.state.runtime

    async def observe():
        async with identity.access() as (session, auth):
            # Simulate provider ORM rows still referenced in a shared unit of work.
            cached = [await session.get(FlowRun, r["id"]) for r in (first, second, third)]
            cached_file = await session.get(File, identifier)
            assert all(r.status == "pending" for r in cached) and cached_file.status == "active"
            # Another committed operation finishes the Runs and disables the file.
            async with rt.database.transaction() as writer:
                await writer.execute(
                    update(FlowRun)
                    .where(FlowRun.id.in_([r["id"] for r in (first, second, third)]))
                    .values(status="failed", finished_at=utcnow(), error_code="test_finished")
                )
                await writer.execute(
                    update(File).where(File.id == identifier).values(status="disabled")
                )
            flows = rt.flow_service(identity)
            replay = await flows.start_in_transaction(
                session,
                auth,
                start_body(identifier, first["request_key"].removeprefix("test-notes:")),
            )
            read = await flows.get_in_transaction(session, auth, second["id"])
            cancelled = await flows.cancel_in_transaction(session, auth, third["id"])
            metadata = await FileService(identity, rt.storage).metadata_in_transaction(
                session, identifier
            )
            assert (
                replay["status"],
                read["status"],
                cancelled["status"],
                cancelled["cancel_requested_at"],
                metadata["status"],
            ) == ("failed", "failed", "failed", None, "disabled")

    browser.portal.call(observe)
