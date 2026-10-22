"""Real native processes, including descendants and loss of the hosting interpreter."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from keystone.core.asyncio import blocking
from keystone.core.errors import ServiceError
from keystone.core.processes import run_process


def python(code, *args):
    return [sys.executable, "-c", code, *map(str, args)]


async def ready(path):
    async with asyncio.timeout(15):
        while not path.exists():
            await asyncio.sleep(0.02)


def tree(tmp_path, *, finish=False):
    heartbeat, gate = tmp_path / "heartbeat", tmp_path / "ready"
    child = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "\nwhile True: p.write_text(str(time.time())); time.sleep(.03)"
    )
    code = (
        "import subprocess,sys,pathlib,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"p=pathlib.Path({str(heartbeat)!r}); "
        "\nwhile not p.exists(): time.sleep(.01)\n"
        f"pathlib.Path({str(gate)!r}).touch(); "
        + ("print('complete',flush=True)" if finish else "time.sleep(600)")
    )
    return python(code), heartbeat, gate


async def stopped(heartbeat):
    # Read after a settling interval: no later writes means no surviving grandchild.
    await asyncio.sleep(0.2)
    first = heartbeat.read_text()
    await asyncio.sleep(0.3)
    assert heartbeat.read_text() == first


async def test_argv_is_literal_cwd_and_nonzero_result(tmp_path, caplog):
    args = ["中文 空格", "; $(secret) & | >", 'quote"value']
    result = await run_process(
        python(
            "import json,os,sys; print(json.dumps([os.getcwd(),sys.argv[1:]])); "
            "sys.stderr.write('private stderr'); sys.exit(7)",
            *args,
        ),
        cwd=tmp_path,
    )
    directory, actual = json.loads(result.stdout)
    assert Path(directory) == tmp_path
    assert actual == args
    assert result.stderr == b"private stderr"
    assert result.returncode == 7
    assert not hasattr(result, "args")
    assert "private stderr" not in caplog.text
    assert "secret" not in caplog.text


async def test_missing_executable_returns_launch_status():
    result = await run_process(["keystone-test-nonexistent-program-984156"])
    assert result.returncode == 127
    assert result.stdout == result.stderr == b""


async def test_reserved_environment_failure_status_is_unavailable():
    with pytest.raises(ServiceError) as error:
        await run_process(python("raise SystemExit(125)"))
    assert (error.value.status, error.value.code) == (503, "process_unavailable")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("wait", [False, True])
async def test_pipe_limits_including_fast_exit(stream, wait):
    code = f"import sys,time; sys.{stream}.buffer.write(b'x'*1048576); "
    code += f"sys.{stream}.flush(); " + ("time.sleep(600)" if wait else "")
    with pytest.raises(ServiceError) as error:
        await run_process(python(code), stdout_limit=1024, stderr_limit=1024, timeout=10)
    assert (error.value.status, error.value.code) == (413, "process_output_limit")
    assert "xxxx" not in error.value.detail


@pytest.mark.parametrize("wait", [False, True])
async def test_file_limit_including_fast_exit(tmp_path, wait):
    output = tmp_path / "result"
    code = f"from pathlib import Path; import time; Path({str(output)!r}).write_bytes(b'x'*65);"
    code += "time.sleep(600)" if wait else ""
    with pytest.raises(ServiceError) as error:
        await run_process(python(code), output=output, output_limit=64)
    assert error.value.code == "process_file_limit"


async def test_zero_and_exact_limits(tmp_path):
    result = await run_process(python("pass"), stdout_limit=0, stderr_limit=0)
    assert result.stdout == result.stderr == b""
    output = tmp_path / "result"
    result = await run_process(
        python(
            f"import sys,pathlib; sys.stdout.write('1234'); "
            f"sys.stderr.write('56'); pathlib.Path({str(output)!r}).touch()"
        ),
        stdout_limit=4,
        stderr_limit=2,
        output=output,
        output_limit=0,
    )
    assert (result.stdout, result.stderr) == (b"1234", b"56")


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": 86401},
        {"stdout_limit": -1},
        {"stderr_limit": 16 * 1024**2 + 1},
        {"stdout_limit": True},
        {"output": "file"},
        {"output": "file", "output_limit": -1},
        {"output_limit": 2},
    ],
)
async def test_invalid_bounds_do_not_launch(options, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid bounds must be rejected before launching a process")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    with pytest.raises(ValueError):
        await run_process(python("pass"), **options)


@pytest.mark.parametrize("argv", [[], "shell string", [""], ["tool", "a\0b"], [123]])
async def test_invalid_argv(argv):
    with pytest.raises(ValueError):
        await run_process(argv)


async def test_launch_failure_does_not_expose_native_error(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private path/token")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(ServiceError) as error:
        await run_process(python("pass"))
    assert error.value.code == "process_unavailable"
    assert "private" not in error.value.detail


async def test_timeout_cleans_descendants(tmp_path):
    argv, heartbeat, _ = tree(tmp_path)
    with pytest.raises(ServiceError) as error:
        await run_process(argv, timeout=2)
    assert error.value.code == "process_timeout"
    await stopped(heartbeat)


async def test_repeated_cancellation_waits_for_tree_cleanup(tmp_path):
    argv, heartbeat, gate = tree(tmp_path)
    task = asyncio.create_task(run_process(argv))
    await ready(gate)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stopped(heartbeat)


async def test_successful_parent_cleans_remaining_descendants(tmp_path):
    argv, heartbeat, _ = tree(tmp_path, finish=True)
    result = await run_process(argv)
    assert result.returncode == 0
    assert result.stdout.strip() == b"complete"
    await stopped(heartbeat)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
async def test_abrupt_supervisor_death_closes_job_and_kills_descendants(tmp_path):
    argv, heartbeat, _ = tree(tmp_path, finish=True)
    argv[2] += "; import os,signal; os.kill(os.getppid(),signal.SIGTERM); time.sleep(600)"
    result = await run_process(argv, timeout=10)
    assert result.returncode != 0
    await stopped(heartbeat)


async def test_host_interpreter_crash_cleans_descendants(tmp_path):
    argv, heartbeat, gate = tree(tmp_path)
    code = (
        "import asyncio,os; from pathlib import Path; "
        "from keystone.core.processes import run_process\n"
        "async def main():\n"
        f" task=asyncio.create_task(run_process({argv!r}))\n"
        f" while not Path({str(gate)!r}).exists(): await asyncio.sleep(.02)\n"
        " os._exit(71)\nasyncio.run(main())"
    )
    process = subprocess.Popen(python(code), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        await ready(gate)
        await asyncio.to_thread(process.wait, 15)
        assert process.returncode == 71, process.stderr.read()
        await stopped(heartbeat)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
        process.stderr.close()


async def test_blocking_repeated_cancel_waits_for_native_release():
    entered, release, finished = Event(), Event(), Event()

    def native():
        entered.set()
        assert release.wait(5)
        finished.set()
        raise OSError("cleanup failure stays behind cancellation")

    task = asyncio.create_task(blocking(native, on_cancel=release.set))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
