import asyncio
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi import WebSocketDisconnect
from pydantic import SecretStr
from sqlalchemy import update
from test_identity import ORIGIN, create_user, login, token
from test_identity import client as client

from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.environments.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.environments.models import EnvironmentSession
from keystone.platform.environments.services.idle import stop_idle
from keystone.platform.environments.services.registration import register_environment
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.files.services.references import registered_file_ids
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext

pytestmark = pytest.mark.postgres


class BackupRuntime:
    content = b"synthetic encrypted archive fixture" * 4

    def __init__(self):
        self.sandboxes = set()
        self.purposes = []
        self.volume_present = False
        self.fail_backup = False
        self.restores = 0
        self.keys = []

    async def create(self, run, workspace, volume, *, purpose="interactive", initialize=False):
        assert not initialize
        identifier = str(run)
        self.purposes.append(purpose)
        self.sandboxes.add(identifier)
        return identifier

    async def wait_profile(self, sandbox, run):
        return "present" if self.volume_present else "missing"

    async def wait_ready(self, sandbox, run):
        assert self.volume_present

    async def exists(self, sandbox):
        return sandbox in self.sandboxes

    async def quit_browser(self, sandbox, run):
        assert sandbox in self.sandboxes

    async def delete(self, sandbox):
        self.sandboxes.discard(sandbox)

    @asynccontextmanager
    async def backup(self, sandbox, run, key, limit):
        self.keys.append(key)
        if self.fail_backup:
            raise RuntimeUnavailable

        async def chunks():
            yield self.content

        yield len(self.content), hashlib.sha256(self.content).hexdigest(), chunks()

    async def restore(self, sandbox, run, key, size, chunks):
        self.keys.append(key)
        assert not self.volume_present
        assert b"".join([chunk async for chunk in chunks]) == self.content
        self.restores += 1
        self.volume_present = True
        return True


def test_backup_replacement_and_restore_only_when_volume_is_missing(client, tmp_path):
    owner = login(client)
    runtime = BackupRuntime()
    client.app.state.opensandbox = runtime
    app = client.app.state.runtime
    settings = Settings(
        config_file=None,
        file_coordination_root=tmp_path / "coordination",
        file_storages={"local": {"driver": "filesystem", "root": tmp_path / "files"}},
        file_min_free_bytes=0,
    )
    app.settings.file_coordination_root = settings.file_coordination_root
    app.settings.file_storages = settings.file_storages
    app.settings.browser_backup_key = SecretStr("a" * 64)
    app.storage = StorageRegistry(settings)
    app.storage.initialize()
    workspace = client.portal.call(
        register_environment,
        app.database,
        app.environment_keys,
        UUID(owner["id"]),
        "chromium",
        "default",
        "browser-backup-test",
    )
    root = "/api/v1/environments"
    path = f"{root}/{workspace}"

    async def current_ref():
        async with app.database.transaction() as session:
            return await registered_file_ids(session, f"environment:{workspace}", "latest_backup")

    missing = client.post(path + "/open")
    assert missing.status_code == 409
    assert missing.json()["code"] == "environment_backup_missing"
    assert not runtime.sandboxes

    runtime.volume_present = True
    first = client.post(path + "/open").json()
    assert first["state"] == "ready" and first["purpose"] == "interactive"
    assert first["restored_from"] is None
    busy = client.post(path + "/backup")
    assert busy.status_code == 409
    stopped = client.post(f"{path}/sessions/{first['id']}/stop").json()
    assert stopped["state"] == "stopped"
    auto_ref = client.portal.call(current_ref)
    assert len(auto_ref) == 1
    assert client.post(path + "/backup").status_code == 200
    assert runtime.purposes[-1] == "backup"
    manual_ref = client.portal.call(current_ref)
    assert len(manual_ref) == 1 and manual_ref != auto_ref
    saved = client.get(root).json()[0]["backup_at"]

    runtime.fail_backup = True
    assert client.post(path + "/backup").status_code == 503
    assert client.portal.call(current_ref) == manual_ref
    view = client.get(root).json()[0]
    assert view["backup_at"] == saved and view["backup_failed"]

    # A present volume is preferred even if a backup exists. Its data is never overwritten.
    current = client.post(path + "/open").json()
    assert current["restored_from"] is None and runtime.restores == 0
    assert client.post(f"{path}/sessions/{current['id']}/stop").json()["state"] == "stopped"
    runtime.volume_present = False
    recovered = client.post(path + "/open").json()
    assert recovered["state"] == "ready"
    assert recovered["restored_from"] == saved and runtime.restores == 1
    assert runtime.keys[0] == runtime.keys[-1]


class Runtime:
    def __init__(self):
        self.started = Event()
        self.release = Event()
        self.create_count = 0
        self.created = None
        self.discoverable = False
        self.exists_now = True
        self.connected = False
        self.stops = []

    async def create(self, run, workspace, volume, *, purpose="interactive", initialize=False):
        self.create_count += 1
        self.created = run, workspace, volume
        self.started.set()
        assert await asyncio.to_thread(self.release.wait, 5)
        raise RuntimeUnavailable

    async def find(self, run, workspace):
        assert (run, workspace) == self.created[:2]
        return "test-sandbox" if self.discoverable else None

    async def exists(self, sandbox):
        assert sandbox == "test-sandbox"
        return self.exists_now

    async def ready(self, sandbox, run):
        assert sandbox == "test-sandbox"
        return self.exists_now

    @asynccontextmanager
    async def stream(self, sandbox, run):
        assert (sandbox, run) == ("test-sandbox", self.created[0])
        frames = asyncio.Queue()

        class Socket:
            async def send(self, frame):
                await frames.put(frame)

            async def __aiter__(self):
                while True:
                    yield await frames.get()

        self.connected = True
        try:
            yield Socket()
        finally:
            self.connected = False

    async def quit_browser(self, sandbox, run):
        assert not self.connected
        self.stops.append((sandbox, run))

    async def delete(self, sandbox):
        assert self.stops and self.stops[-1][0] == sandbox
        self.exists_now = False


def test_unknown_create_reuses_reservation_and_stop_drains_control(client):
    owner = login(client)
    outsider = create_user(client)
    database = client.app.state.runtime.database
    keys = client.app.state.runtime.environment_keys
    workspace = client.portal.call(
        register_environment,
        database,
        keys,
        UUID(owner["id"]),
        "chromium",
        "default",
        "browser-test-volume",
    )
    other = client.portal.call(
        register_environment,
        database,
        keys,
        UUID(outsider["id"]),
        "chromium",
        "default",
        "other-test-volume",
    )
    runtime = Runtime()
    client.app.state.opensandbox = runtime
    root = "/api/v1/environments"
    path = f"{root}/{workspace}"

    assert [item["id"] for item in client.get(root).json()] == [str(workspace)]
    assert client.post(f"{root}/{other}/open").status_code == 404

    with ThreadPoolExecutor(1) as executor:
        opening = executor.submit(client.post, path + "/open")
        try:
            assert runtime.started.wait(5)
            duplicate = client.post(path + "/open").json()
            assert duplicate["state"] == "starting"
        finally:
            runtime.release.set()
        initial = opening.result(timeout=5).json()
    assert initial["id"] == duplicate["id"]
    assert initial["state"] == "review"
    run = f"{path}/sessions/{initial['id']}"
    assert client.post(run + "/check").json()["state"] == "review"
    assert client.post(path + "/open").json()["id"] == initial["id"]
    assert runtime.create_count == 1

    runtime.discoverable = True
    assert client.post(run + "/check").json()["state"] == "ready"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(run + "/screen", headers={"Origin": "https://other"}):
            pytest.fail("Untrusted origin was accepted")
    with client.websocket_connect(run + "/screen", headers={"Origin": ORIGIN}) as screen:
        screen.send_bytes(b"frame")
        assert screen.receive_bytes() == b"frame"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(run + "/screen", headers={"Origin": ORIGIN}):
                pytest.fail("Second controller was accepted")
        stopped = client.post(run + "/stop")
        assert stopped.json()["state"] == "stopped", stopped.text
        assert not runtime.connected
        assert len(runtime.stops) == 1


class FirstUseRuntime:
    def __init__(self):
        self.initialized = set()
        self.creates = []
        self.sandboxes = {}

    async def create(self, run, environment, volume, *, purpose="interactive", initialize=False):
        self.creates.append((volume, initialize))
        if initialize:
            self.initialized.add(volume)
        self.sandboxes[str(run)] = volume
        return str(run)

    async def wait_profile(self, sandbox, run):
        return "present" if self.sandboxes[sandbox] in self.initialized else "missing"

    async def wait_ready(self, sandbox, run):
        assert self.sandboxes[sandbox] in self.initialized

    async def exists(self, sandbox):
        return sandbox in self.sandboxes

    async def ready(self, sandbox, run):
        return sandbox in self.sandboxes

    async def close(self):
        pass

    async def quit_browser(self, sandbox, run):
        pass

    async def delete(self, sandbox):
        self.sandboxes.pop(sandbox, None)

    @asynccontextmanager
    async def stream(self, sandbox, run):
        class Socket:
            async def send(self, frame):
                pass

            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b""

        yield Socket()


def test_default_environment_is_created_on_first_use_and_initialized_once(client):
    login(client)
    runtime = FirstUseRuntime()
    client.app.state.opensandbox = runtime
    root = "/api/v1/environments"

    [placeholder] = client.get(root).json()
    assert placeholder["id"] is None and placeholder["key"] == "default"
    assert client.post(root, json={"kind": "chromium", "key": "missing"}).status_code == 404

    first = client.post(root, json={"kind": "chromium", "key": "default"}).json()
    again = client.post(root, json={"kind": "chromium", "key": "default"}).json()
    assert first["id"] == again["id"] and first["name"] == "默认环境"
    assert [item["id"] for item in client.get(root).json()] == [first["id"]]

    path = f"{root}/{first['id']}"
    opened = client.post(path + "/open").json()
    assert opened["state"] == "ready"
    assert client.post(f"{path}/sessions/{opened['id']}/stop").json()["state"] == "stopped"
    # After the first ready, a lost volume is never initialized again.
    runtime.initialized.clear()
    missing = client.post(path + "/open")
    assert missing.status_code == 409
    assert missing.json()["code"] == "environment_state_missing"
    assert [initialize for _, initialize in runtime.creates] == [True, False]


def _ready_default(client):
    runtime = FirstUseRuntime()
    client.app.state.opensandbox = runtime
    created = client.post("/api/v1/environments", json={"kind": "chromium", "key": "default"})
    path = f"/api/v1/environments/{created.json()['id']}"
    opened = client.post(path + "/open").json()
    assert opened["state"] == "ready"
    return runtime, path, UUID(opened["id"])


def _update_session(client, session_id, **values):
    async def change():
        async with client.app.state.runtime.database.transaction() as session:
            await session.execute(
                update(EnvironmentSession)
                .where(EnvironmentSession.id == session_id)
                .values(**values)
            )

    client.portal.call(change)


def test_idle_sessions_stop_in_maintenance_unless_held_or_recent(client):
    login(client)
    runtime, path, session_id = _ready_default(client)
    database = client.app.state.runtime.database

    assert client.portal.call(stop_idle, database, runtime) == 0
    _update_session(client, session_id, last_activity_at=utcnow() - timedelta(hours=1))
    _update_session(
        client,
        session_id,
        control_owner=uuid4(),
        control_kind="automation",
        control_until=utcnow() + timedelta(minutes=1),
    )
    assert client.portal.call(stop_idle, database, runtime) == 0
    stop = client.post(f"{path}/sessions/{session_id}/stop")
    assert stop.status_code == 409 and stop.json()["code"] == "environment_in_use"

    # A hold from a crashed process lapses on its own.
    _update_session(client, session_id, control_until=utcnow() - timedelta(seconds=1))
    assert client.portal.call(stop_idle, database, runtime) == 1
    [view] = client.get("/api/v1/environments").json()
    assert view["session"]["state"] == "stopped" and not runtime.sandboxes


def test_screen_hold_is_shared_across_processes_and_released_on_close(client):
    login(client)
    _, path, session_id = _ready_default(client)
    screen = f"{path}/sessions/{session_id}/screen"
    other = uuid4()
    _update_session(
        client,
        session_id,
        control_owner=other,
        control_kind="human",
        control_until=utcnow() + timedelta(minutes=1),
    )
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(screen, headers={"Origin": ORIGIN}):
            pytest.fail("A hold recorded by another process was ignored")

    _update_session(client, session_id, control_until=utcnow() - timedelta(seconds=1))

    async def holder():
        async with client.app.state.runtime.database.transaction() as session:
            run = await session.get(EnvironmentSession, session_id)
            return run.control_owner, run.control_kind

    with client.websocket_connect(screen, headers={"Origin": ORIGIN}):
        owner, kind = client.portal.call(holder)
        assert owner not in {None, other} and kind == "human"
    deadline = time.monotonic() + 5
    while client.portal.call(holder) != (None, None):
        assert time.monotonic() < deadline
        time.sleep(0.05)


class ActingRuntime(FirstUseRuntime):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.url = "https://portal.example.com/home"
        self.fail = False

    async def action(self, sandbox, run, name, arguments, timeout_seconds):
        self.actions.append((name, arguments))
        if self.fail:
            raise RuntimeUnavailable
        if name == "open":
            self.url = arguments["url"]
        return {"state": "ok", "url": self.url, "cookies": "never returned"}


def test_automation_holds_registers_actions_and_enforces_scope(client):
    login(client)
    runtime = ActingRuntime()
    client.app.state.opensandbox = runtime
    app = client.app.state.runtime
    app.opensandbox = runtime
    secret = token(client, ["environment:use"])["token"]
    identity = AccessContext(app.database, app.settings, Credential("pat", secret))
    automation = app.environment_automation(identity)
    flow, other = uuid4(), uuid4()
    act = partial(automation.act, domains=("example.com",))

    def fails(code, *call):
        with pytest.raises(ServiceError) as error:
            client.portal.call(*call)
        assert error.value.code == code

    grant = client.portal.call(automation.acquire, "chromium", "default", flow)
    assert client.portal.call(automation.acquire, "chromium", "default", flow) == grant
    fails("environment_in_use", automation.acquire, "chromium", "default", other)

    # The screen cannot take input while automation holds the session.
    path = f"/api/v1/environments/{grant.environment_id}/sessions/{grant.session_id}"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path + "/screen", headers={"Origin": ORIGIN}):
            pytest.fail("Screen took input during automation")
    assert client.post(path + "/stop").json()["code"] == "environment_in_use"

    result = client.portal.call(act, grant, 1, "snapshot", None)
    assert result == {"state": "ok", "url": "https://portal.example.com/home"}
    fails("environment_action_unknown", act, grant, 1, "snapshot", None)
    fails("environment_action_sequence", act, grant, 3, "snapshot", None)

    # A lost outcome keeps the hold busy; the same number is never sent again.
    runtime.fail = True
    fails("environment_action_unknown", act, grant, 2, "click", {"ref": "s1e1"})
    runtime.fail = False
    sent = len(runtime.actions)
    fails("environment_action_unknown", act, grant, 2, "click", {"ref": "s1e1"})
    assert len(runtime.actions) == sent
    client.portal.call(act, grant, 3, "read", None)

    fails("environment_scope_violation", act, grant, 4, "open", {"url": "https://elsewhere.test/"})
    fails("environment_control_lost", act, grant, 5, "snapshot", None)

    # After the hold ends the user can take over the same session.
    with client.websocket_connect(path + "/screen", headers={"Origin": ORIGIN}):
        pass
    grant = client.portal.call(automation.acquire, "chromium", "default", other)
    client.portal.call(automation.release, grant)
    with client.websocket_connect(path + "/screen", headers={"Origin": ORIGIN}):
        pass
