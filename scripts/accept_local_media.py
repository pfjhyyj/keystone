"""Opt-in real HTTP/RSS/video acceptance; FFmpeg and console Playwright are test tools only."""

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from alembic import command
from psycopg import sql

from keystone.composition import build_catalog
from keystone.core.asyncio import run
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.migrations import migration_config
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.identity.adapters.security import Passwords
from keystone.platform.identity.schemas import UserCreate
from keystone.platform.identity.services.initialize import initialize_admin

ROOT = Path(__file__).resolve().parents[1]


def peak_rss(pid):
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "working",
                    "pool_peak",
                    "pool",
                    "nonpaged_peak",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        kernel, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            raise ctypes.WinError()
        try:
            counters = Counters()
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            ):
                raise ctypes.WinError()
            return counters.peak
        finally:
            kernel.CloseHandle(handle)
    values = Path(f"/proc/{pid}/status").read_text().splitlines()
    return int(next(line.split()[1] for line in values if line.startswith("VmHWM:"))) * 1024


def exercise(root, database_url, channel):
    from support.processes import stop_process

    ffmpeg, node = shutil.which("ffmpeg"), shutil.which("node")
    if not ffmpeg or not node:
        raise RuntimeError("Install FFmpeg and Node.js to run this optional media acceptance")
    storage = root / "storage"
    settings = Settings(
        config_file=None,
        env="test",
        database_url=database_url,
        file_coordination_root=storage,
        file_storages={"local": {"driver": "filesystem", "root": storage}},
    )
    StorageRegistry(settings).initialize()
    config = migration_config(settings)
    config.attributes["create_schema"] = True
    command.upgrade(config, "head")

    async def seed():
        database = Database(settings, catalog=build_catalog())
        try:
            await initialize_admin(
                database,
                Passwords(),
                UserCreate(
                    username="admin",
                    display_name="Acceptance",
                    password="test-password-123",
                ),
            )
        finally:
            await database.close()

    run(seed())
    video = root / "sample.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(video),
        ],
        check=True,
        timeout=60,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {key: value for key, value in os.environ.items() if not key.startswith("KEYSTONE_")}
    env.update(
        {
            "KEYSTONE_ENV": "test",
            "KEYSTONE_DATABASE_URL": database_url,
            "KEYSTONE_FILE_COORDINATION_ROOT": str(storage),
            "KEYSTONE_FILE_STORAGES__LOCAL__DRIVER": "filesystem",
            "KEYSTONE_FILE_STORAGES__LOCAL__ROOT": str(storage),
            "KEYSTONE_FILE_MAX_BYTES": str(512 * 1024**2),
        }
    )
    pid_file = root / "api.pid"
    code = f"""
import os
from pathlib import Path
import uvicorn
from keystone.bootstrap import create_app
from keystone.core.config import Settings
Path({str(pid_file)!r}).write_text(str(os.getpid()))
uvicorn.run(create_app(Settings(config_file=None)), host='127.0.0.1', port={port},
    loop='keystone.core.asyncio:new_event_loop', access_log=False)
"""
    with (root / "api.log").open("w", encoding="utf8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", code], env=env, stdout=output, stderr=output
        )
        try:
            with httpx.Client(base_url=base, timeout=180) as client:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if client.get("/api/v1/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError((root / "api.log").read_text(encoding="utf8"))
                    time.sleep(0.1)
                headers = {"Origin": "http://127.0.0.1:5173", "X-Keystone-Request": "1"}
                login = client.post(
                    "/api/v1/auth/login",
                    headers=headers,
                    json={
                        "username": "admin",
                        "password": "test-password-123",
                    },
                )
                login.raise_for_status()
                headers["X-CSRF-Token"] = login.json()["csrf_token"]
                pid, measurements = int(pid_file.read_text()), []
                for mib in (16, 256):
                    source = root / f"sample-{mib}.mp4"
                    shutil.copyfile(video, source)
                    padding = mib * 1024**2 - source.stat().st_size
                    # A legal MP4 free box gives deterministic I/O scale, retaining playable media.
                    with source.open("ab") as stream:
                        stream.write(struct.pack(">I4s", padding, b"free"))
                        remaining = padding - 8
                        while remaining:
                            chunk = bytes(min(256 * 1024, remaining))
                            stream.write(chunk)
                            remaining -= len(chunk)
                    with source.open("rb") as stream:
                        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                    params = {
                        "name": source.name,
                        "expected_size": source.stat().st_size,
                        "content_type": "video/mp4",
                        "checksum_sha256": checksum,
                        "request_key": f"media-{mib}",
                    }
                    before, started = peak_rss(pid), time.monotonic()
                    with source.open("rb") as stream:
                        receipt = client.post(
                            "/api/v1/files",
                            params=params,
                            headers=headers,
                            content=stream,
                        )
                    receipt.raise_for_status()
                    identifier = receipt.json()["id"]
                    url = f"/api/v1/files/{identifier}/content"
                    digest, size = hashlib.sha256(), 0
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        for chunk in response.iter_bytes(256 * 1024):
                            digest.update(chunk)
                            size += len(chunk)
                    assert size == mib * 1024**2 and digest.hexdigest() == checksum
                    assert client.head(url).headers["content-length"] == str(size)
                    assert client.get(url, headers={"Range": "bytes=-1024"}).status_code == 206
                    measurements.append(
                        {
                            "file_mib": mib,
                            "peak_rss_bytes": peak_rss(pid),
                            "peak_growth_bytes": peak_rss(pid) - before,
                            "roundtrip_seconds": round(time.monotonic() - started, 2),
                        }
                    )
                assert (
                    measurements[1]["peak_rss_bytes"] - measurements[0]["peak_rss_bytes"]
                    < 32 * 1024**2
                )
                browser_env = {
                    **os.environ,
                    "MEDIA_URL": base + url,
                    "MEDIA_COOKIE": client.cookies["keystone_session"],
                    "MEDIA_CHANNEL": channel,
                }
                browser = subprocess.run(
                    [node, str(ROOT / "scripts/accept_local_media.cjs")],
                    env=browser_env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if browser.returncode:
                    raise RuntimeError(browser.stderr)
                return {
                    "measurements": measurements,
                    "browser": json.loads(browser.stdout),
                    "input": "8-second H264 MP4 with free-box padding",
                    "status": "passed",
                }
        finally:
            stop_process(process, timeout=20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser-channel", default="", help="Optional system browser, e.g. msedge"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "backend/tests"))
    from support.postgres import probe_test_database

    probe = probe_test_database()
    if not probe.available:
        raise SystemExit(f"Local media acceptance requires a test database: {probe.detail}")
    url = probe.url
    assert url is not None
    name = "keystone_test_" + uuid4().hex
    result_path = ROOT / ".artifacts/local-media" / f"{name}.json"
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=5,
    ) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with tempfile.TemporaryDirectory(prefix="keystone_media_") as temporary:
                result = exercise(
                    Path(temporary),
                    url.set(database=name).render_as_string(hide_password=False),
                    args.browser_channel,
                )
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(json.dumps(result, indent=2), encoding="utf8")
                print(result_path)
                print(json.dumps(result, indent=2))
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    main()
