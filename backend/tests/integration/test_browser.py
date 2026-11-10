import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Event
from uuid import UUID

import pytest
from fastapi import WebSocketDisconnect
from pydantic import SecretStr
from test_identity import ORIGIN, create_user, login
from test_identity import client as client

from keystone.core.config import Settings
from keystone.platform.browser.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.browser.services.registration import register_workspace
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.files.services.references import registered_file_ids

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

    async def create(self, run, workspace, volume, *, purpose="browse"):
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
    client.app.state.browser_runtime = runtime
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
        register_workspace, app.database, UUID(owner["id"]), "备份空间", "browser-backup-test"
    )
    root = "/api/v1/browser-workspaces"
    path = f"{root}/{workspace}"

    async def current_ref():
        async with app.database.transaction() as session:
            return await registered_file_ids(session, f"browser:{workspace}", "latest_backup")

    missing = client.post(path + "/open")
    assert missing.status_code == 409
    assert missing.json()["code"] == "browser_backup_missing"
    assert not runtime.sandboxes

    runtime.volume_present = True
    first = client.post(path + "/open").json()
    assert first["state"] == "ready" and first["purpose"] == "browse"
    assert first["restored_from"] is None
    busy = client.post(path + "/backup")
    assert busy.status_code == 409
    stopped = client.post(f"{path}/runs/{first['id']}/stop").json()
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
    assert client.post(f"{path}/runs/{current['id']}/stop").json()["state"] == "stopped"
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

    async def create(self, run, workspace, volume, *, purpose="browse"):
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
    workspace = client.portal.call(
        register_workspace, database, UUID(owner["id"]), "工作空间", "browser-test-volume"
    )
    other = client.portal.call(
        register_workspace, database, UUID(outsider["id"]), "其他空间", "other-test-volume"
    )
    runtime = Runtime()
    client.app.state.browser_runtime = runtime
    root = "/api/v1/browser-workspaces"
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
    run = f"{path}/runs/{initial['id']}"
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
