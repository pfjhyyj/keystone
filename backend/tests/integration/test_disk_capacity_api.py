from types import SimpleNamespace

import pytest
from test_files_dictionary import client as client
from test_files_dictionary import stored_files, upload

from keystone.platform.files.adapters import capacity as disk

pytestmark = pytest.mark.postgres


def test_disk_pressure_does_not_block_reading_existing_content(client, monkeypatch):
    file = upload(client, b"0123456789").json()
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    response = client.request(
        "GET", f"/api/v1/_test/materials/{file['id']}/content", headers={"Range": "bytes=2-5"}
    )
    assert response.status_code == 206 and response.content == b"2345"


def test_upload_rejects_missing_final_copy_capacity_before_registering_file(client, monkeypatch):
    settings = client.app.state.runtime.settings
    monkeypatch.setattr(
        disk.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=settings.file_min_free_bytes + 9),
    )
    response = upload(client, b"hello")
    assert response.status_code == 507
    assert response.json()["code"] == "file_capacity_full"
    assert stored_files(client) == []
    root = settings.file_coordination_root
    assert not list((root / ".capacity").iterdir())
    assert not list((root / ".transfers").iterdir())


def test_disk_pressure_at_final_copy_returns_507_and_retains_retryable_file(client, monkeypatch):
    runtime = client.app.state.runtime
    state = SimpleNamespace(free=runtime.settings.file_min_free_bytes + 10)
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda path: state)
    source = runtime.storage.get()
    write = source.write

    def pressure(*args):
        state.free = 0
        return write(*args)

    monkeypatch.setattr(source, "write", pressure)
    response = upload(client, b"hello")
    assert response.status_code == 507
    assert response.json()["code"] == "file_capacity_full"
    files = stored_files(client)
    assert len(files) == 1 and files[0]["status"] == "failed"
    root = runtime.settings.file_coordination_root
    assert not list((root / ".capacity").iterdir())
    assert not list((root / ".transfers").iterdir())
    # Existing metadata/request identity survives; cleanup must not delete business files.
    monkeypatch.setattr(source, "write", write)
    state.free = runtime.settings.file_min_free_bytes + 10
    retried = upload(client, b"hello")
    assert retried.status_code == 201
    assert retried.json()["id"] == files[0]["id"]
    assert retried.json()["status"] == "active"
