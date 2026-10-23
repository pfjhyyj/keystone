"""Bounded process-tree shutdown for tests that launch real workers."""

import subprocess
import sys


def stop_process(process, *, timeout=10):
    """Stop a process tree, falling back when taskkill is blocked by the sandbox."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            try:
                process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                pass
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
