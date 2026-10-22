"""Bounded argv execution for trusted host code, independent of Flow and business policy."""

import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

from keystone.core.asyncio import blocking
from keystone.core.errors import ServiceError


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_process(
    arguments,
    *,
    timeout=300,
    stdout_limit=256 * 1024,
    stderr_limit=64 * 1024,
    output=None,
    output_limit=None,
    cwd=None,
):
    """No shell, automatic retries, business error mapping or raw-output logging.

    Trusted code chooses executable, arguments and paths. This is resource/lifetime
    containment, not a sandbox for untrusted code. Call inside a Flow execution scope
    when invoking on behalf of a Run. Native cleanup finishes before cancellation returns.
    """
    if isinstance(arguments, (str, bytes)):
        raise ValueError("Arguments must be a nonempty argv sequence")
    try:
        argv = [os.fspath(value) for value in arguments]
    except TypeError:
        raise ValueError("Invalid process arguments") from None
    if (
        not argv
        or any(not isinstance(value, str) or "\0" in value for value in argv)
        or not argv[0]
    ):
        raise ValueError("Invalid process arguments")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError("Timeout must be between 0 and 86400 seconds")
    for limit in (stdout_limit, stderr_limit):
        if type(limit) is not int or not 0 <= limit <= 16 * 1024**2:
            raise ValueError("Output buffers must be bounded to 16 MiB")
    if output is not None:
        if type(output_limit) is not int or output_limit < 0:
            raise ValueError("An output path requires a nonnegative size limit")
        output = Path(output)
    elif output_limit is not None:
        raise ValueError("An output size limit requires a path")
    cancelled = Event()

    def execute():
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "keystone.core.process_supervisor", *argv],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=cwd,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                start_new_session=sys.platform != "win32",
            )
        except OSError:
            raise ServiceError(503, "process_unavailable", "无法启动受限子进程") from None
        buffers, overflow, io_failed = [bytearray(), bytearray()], Event(), Event()

        def read(stream, data, maximum):
            try:
                while chunk := stream.read(4096):
                    if len(data) + len(chunk) > maximum:
                        overflow.set()
                    elif not overflow.is_set():
                        data.extend(chunk)
                    # Drain even after overflow, so shutdown never blocks on a full pipe.
            except OSError:
                io_failed.set()

        readers = [
            Thread(target=read, args=(stream, data, maximum), daemon=True)
            for stream, data, maximum in zip(
                (process.stdout, process.stderr), buffers, (stdout_limit, stderr_limit), strict=True
            )
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout

        def check_limits():
            if overflow.is_set():
                raise ServiceError(413, "process_output_limit", "子进程输出超过上限")
            if io_failed.is_set():
                raise ServiceError(503, "process_unavailable", "子进程输出读取失败")
            try:
                oversized = (
                    output is not None and output.exists() and output.stat().st_size > output_limit
                )
            except OSError:
                raise ServiceError(503, "process_unavailable", "子进程产物状态不可用") from None
            if oversized:
                raise ServiceError(413, "process_file_limit", "子进程产物超过容量上限")

        try:
            while True:
                if cancelled.is_set():
                    return None
                check_limits()
                if process.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    raise ServiceError(504, "process_timeout", "子进程执行超时")
                cancelled.wait(0.05)
            for reader in readers:
                reader.join(5)
            check_limits()  # Catch fast writers and bytes buffered at process exit too.
            if process.returncode == 125:
                raise ServiceError(503, "process_unavailable", "子进程执行环境不可用")
            return ProcessResult(process.returncode, bytes(buffers[0]), bytes(buffers[1]))
        finally:
            process.stdin.close()  # EOF kills/reaps the tool and its descendants.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for reader in readers:
                reader.join(5)
            process.stdout.close()
            process.stderr.close()
            if any(reader.is_alive() for reader in readers):
                raise ServiceError(503, "process_unavailable", "子进程资源未能完成释放")

    return await blocking(execute, on_cancel=cancelled.set)
