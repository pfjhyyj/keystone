"""Native child lifetime follows the host's stdin pipe; no application or media imports."""

import os
import signal
import subprocess
import sys
from threading import Thread


def windows_job():
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", Counters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        raise OSError("Cannot contain native process")
    # Assign before spawning: every descendant inherits containment without a startup race.
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise OSError("Cannot contain native process")

    def terminate(code):
        # Includes this supervisor. Its job handle also closes on abrupt supervisor death.
        kernel.TerminateJobObject(job, code & 0xFFFFFFFF)

    return terminate


def main():
    stop_job = None
    if sys.platform == "win32":
        try:
            stop_job = windows_job()
        except OSError:
            return 125  # Fail closed; do not start an uncontained command.
    try:
        child = subprocess.Popen(
            sys.argv[1:],
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
    except FileNotFoundError:
        return 127
    except OSError:
        return 125

    def stop(*_):
        if stop_job is not None:
            stop_job(1)
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def parent_gone():
        os.read(sys.stdin.fileno(), 1)
        stop()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, stop)
    Thread(target=parent_gone, daemon=True).start()
    code = child.wait()
    if stop_job is not None:
        stop_job(code)
    else:
        stop()  # A completed command may have left descendants holding output pipes.
    return code


if __name__ == "__main__":
    raise SystemExit(main())
