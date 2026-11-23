"""Run init, API, Worker and console in one terminal against an already running database."""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
CONSOLE = ROOT / "frontend/console"
CONFIG = BACKEND / "keystone.toml"
WINDOWS = os.name == "nt"
COLORS = {"api": "36", "worker": "35", "web": "32"}


def executable(name):
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"{name} is required on PATH")
    return path


def run(command, cwd):
    if subprocess.run(command, cwd=cwd).returncode:
        raise SystemExit(f"failed: {' '.join(command)}")


def reachable(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


def prepare(args):
    if not CONFIG.exists():
        shutil.copyfile(BACKEND / "keystone.toml.example", CONFIG)
        print("created backend/keystone.toml from the example")
    database_url = os.environ.get("KEYSTONE_DATABASE_URL") or tomllib.loads(
        CONFIG.read_text(encoding="utf-8")
    ).get("database_url", "")
    # Starting a database is the developer's choice; only report how to reach one.
    address = urlsplit(database_url)
    if not reachable(address.hostname or "127.0.0.1", address.port or 5432):
        raise SystemExit(
            f"database {address.hostname}:{address.port} is not reachable; start it first,"
            " e.g. docker compose -f deploy/compose.yaml up -d --wait,"
            " or set database_url in backend/keystone.toml"
        )
    uv = executable("uv")
    run([uv, "sync", "--locked"], BACKEND)
    # Applies new migrations after a branch switch; prompts for the admin password only once.
    run([uv, "run", "--locked", "keystone", "init"], BACKEND)
    if "web" not in args.skip and not (CONSOLE / "node_modules").exists():
        run([executable("pnpm"), "install", "--frozen-lockfile"], CONSOLE)


def pipe(name, process, width):
    prefix = f"\x1b[{COLORS[name]}m{name.ljust(width)} |\x1b[0m "
    for line in iter(process.stdout.readline, b""):
        sys.stdout.write(prefix + line.decode("utf-8", "replace").rstrip("\r\n") + "\n")
        sys.stdout.flush()


def start(name, command, cwd, width):
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "FORCE_COLOR": "1"}
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if WINDOWS
        else {"start_new_session": True}
    )
    # No terminal input: cmd shims (pnpm.cmd) then stop without a "terminate batch job" prompt.
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **options,
    )
    process.reader = threading.Thread(target=pipe, args=(name, process, width), daemon=True)
    process.reader.start()
    return process


def stop(processes):
    for process in processes.values():
        if process.poll() is None:
            if WINDOWS:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
    for name, process in processes.items():
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print(f"{name} did not stop in 15s; killing it")
            if WINDOWS:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.reader.join(timeout=2)


def interrupt(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip",
        action="append",
        choices=sorted(COLORS),
        default=[],
        help="Leave a process to be started elsewhere, e.g. under an IDE debugger",
    )
    args = parser.parse_args()
    # Child output is UTF-8; an unencodable character must not kill a log thread.
    sys.stdout.reconfigure(errors="replace")
    if WINDOWS:
        os.system("")  # Enable ANSI colors in the Windows console.
    # Closing the terminal or a polite kill stops the children like Ctrl+C does.
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    prepare(args)
    uv = executable("uv")
    commands = {
        "api": ([uv, "run", "--locked", "keystone-api"], BACKEND),
        "worker": ([uv, "run", "--locked", "keystone-worker"], BACKEND),
        "web": ([executable("pnpm"), "dev"], CONSOLE),
    }
    names = [name for name in commands if name not in args.skip]
    width = max(map(len, names), default=0)
    processes = {name: start(name, *commands[name], width) for name in names}
    print("console: http://127.0.0.1:5173/console/  (Ctrl+C stops everything)")
    try:
        while all(process.poll() is None for process in processes.values()):
            threading.Event().wait(0.5)
        exited = next(name for name, process in processes.items() if process.poll() is not None)
        print(f"{exited} exited with {processes[exited].returncode}; stopping the rest")
    except KeyboardInterrupt:
        print("stopping")
    finally:
        stop(processes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
