import fcntl
import hmac
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic, sleep

from cryptography.exceptions import InvalidTag

from archive import BLOCK, MAX_ARCHIVE, export, restore

PROFILE = Path("/data/profile")
MARKER = PROFILE / ".keystone-profile"


def ready_port(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def main():
    user = pwd.getpwnam("browser")
    if sys.argv[1:] == ["--init-profile"]:
        os.chown(PROFILE, user.pw_uid, user.pw_gid)
        PROFILE.chmod(0o700)
        MARKER.touch(exist_ok=True)
        return

    if sys.argv[1:] not in ([], ["--maintenance"]):
        raise SystemExit("Unknown browser runtime mode")
    maintenance = sys.argv[1:] == ["--maintenance"]
    restored = PROFILE / "restored"
    profile = None
    if MARKER.is_file() and not MARKER.is_symlink():
        profile = PROFILE
    elif (not restored.is_symlink() and (restored / MARKER.name).is_file()
          and not (restored / MARKER.name).is_symlink()):
        profile = restored
    def staging(path):
        return path.name.startswith(".keystone-stage-") and not path.is_symlink() and path.is_dir()

    missing = profile is None and all(
        (p.name == ".keystone-writer" and not p.is_symlink() and p.is_file()) or staging(p)
        for p in PROFILE.iterdir()
    )
    if missing:
        os.chown(PROFILE, user.pw_uid, user.pw_gid)
        PROFILE.chmod(0o700)
    token = os.environ.pop("KEYSTONE_BROWSER_CONTROL").encode()
    os.environ.pop("EXECD_ACCESS_TOKEN", None)
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    os.environ.update(HOME=user.pw_dir, USER=user.pw_name, DISPLAY=":99")
    # Hold a volume-level lock for the lifetime of this managed browser. Once acquired,
    # a previous managed writer is gone and Chromium's container-specific links are stale.
    lock = None
    if profile is not None or missing:
        fd = os.open(PROFILE / ".keystone-writer", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, "a")
    try:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Browser profile volume already has a writer") from None
    if missing:
        for path in PROFILE.iterdir():
            if staging(path):
                shutil.rmtree(path)
    children = []
    chrome = None
    display = proxy = None

    def start(*args, env=None):
        child = subprocess.Popen(args, env=env)
        children.append(child)
        return child

    def stop_chromium():
        if chrome is not None and chrome.poll() is None:
            windows = subprocess.run(
                ["wmctrl", "-lp"], capture_output=True, text=True, check=True, timeout=1
            ).stdout.splitlines()
            for line in windows:
                fields = line.split(maxsplit=4)
                if len(fields) > 2 and fields[2] == str(chrome.pid):
                    subprocess.run(["wmctrl", "-ic", fields[0]], timeout=1, check=True)
            chrome.wait(timeout=5)

    def shutdown(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def start_browser():
        nonlocal chrome, display, proxy
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = profile / name
            if path.is_symlink():
                path.unlink()
        display = start(
            "Xtigervnc", ":99", "-geometry", "1440x900", "-depth", "24", "-nolisten", "tcp",
            "-localhost", "-SecurityTypes", "None", "-rfbport", "5900", "-NeverShared",
            "-DisconnectClients=0", "-SendCutText=0", "-SetPrimary=0", "-AcceptSetDesktopSize=0",
        )
        deadline = monotonic() + 4
        while subprocess.run(["xdpyinfo"], capture_output=True, timeout=1).returncode:
            if display.poll() is not None or monotonic() >= deadline:
                raise SystemExit("Virtual display failed to start")
            sleep(0.1)
        start("openbox")
        proxy = start(
            "websockify", "--auth-plugin", "auth.BrowserRuntimeAuth", "127.0.0.1:6080",
            "127.0.0.1:5900",
            env={**os.environ, "KEYSTONE_BROWSER_CONTROL": token.decode(),
                 "PYTHONPATH": "/opt/browser-runtime"},
        )
        chrome = start(
            "chromium", f"--user-data-dir={profile}", "--no-first-run", "--start-maximized",
            "--disable-dev-shm-usage", "--disable-background-mode", "--restore-last-session",
        )

    try:
        if profile is not None and not maintenance:
            start_browser()
        window_seen = False

        class Control(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, body=b""):
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                return hmac.compare_digest(
                    self.headers.get("X-Keystone-Browser-Control", "").encode(), token
                )

            def backup_key(self):
                key = bytes.fromhex(self.headers.get("X-Keystone-Browser-Backup-Key", ""))
                if len(key) != 32:
                    raise ValueError("Invalid backup key")
                return key

            def send_backup(self):
                if profile is None or (chrome is not None and chrome.poll() is None):
                    self.reply(409)
                    return
                with tempfile.TemporaryFile() as archive:
                    try:
                        limit = min(
                            MAX_ARCHIVE,
                            int(self.headers.get("X-Keystone-Browser-Backup-Limit", "0")),
                        )
                        size, digest = export(profile, self.backup_key(), limit, archive)
                    except (ValueError, OSError):
                        self.reply(422)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("X-Keystone-Browser-Checksum", digest)
                    self.end_headers()
                    while chunk := archive.read(BLOCK):
                        self.wfile.write(chunk)

            def do_GET(self):
                nonlocal window_seen
                if self.path in {"/profile", "/backup"}:
                    if not self.authorized():
                        self.reply(403)
                    elif self.path == "/backup":
                        self.send_backup()
                    elif profile is not None:
                        self.reply(200, b'{"status":"present"}')
                    elif missing:
                        self.reply(200, b'{"status":"missing"}')
                    else:
                        self.reply(200, b'{"status":"invalid"}')
                    return
                if self.path != "/health":
                    self.reply(404)
                    return
                if chrome is None or chrome.poll() is not None or any(
                    p.poll() is not None for p in (display, proxy)
                ):
                    self.reply(503)
                    return
                if not window_seen:
                    windows = subprocess.run(
                        ["xwininfo", "-root", "-tree"], capture_output=True, timeout=1
                    ).stdout.lower()
                    window_seen = b"chromium" in windows
                if (window_seen and (profile / "SingletonSocket").is_socket()
                        and ready_port(6080) and ready_port(5900)):
                    self.reply(200, b'{"status":"ready"}')
                else:
                    self.reply(503)

            def do_POST(self):
                nonlocal profile
                if self.path not in {"/stop", "/restore"}:
                    self.reply(404)
                    return
                if not self.authorized():
                    self.reply(403)
                    return
                if self.path == "/restore":
                    if profile is not None or not missing or maintenance:
                        self.reply(409)
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 33 < length <= MAX_ARCHIVE:
                            raise ValueError("Invalid archive size")
                        restore(PROFILE, self.backup_key(), self.rfile, length, MARKER.name)
                    except (ValueError, OSError, InvalidTag, EOFError, tarfile.TarError):
                        self.reply(422)
                        return
                    profile = restored
                    start_browser()
                    self.reply(204)
                    return
                try:
                    stop_chromium()
                except subprocess.SubprocessError:
                    self.reply(503)
                else:
                    self.reply(204)

        class ControlServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(8)
                return connection, address

        with ControlServer(("127.0.0.1", 6081), Control) as server:
            server.serve_forever(poll_interval=0.25)
    finally:
        try:
            stop_chromium()
        except subprocess.SubprocessError:
            pass
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
