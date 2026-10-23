"""Native file leases and cancellation guarantees require no database."""

import asyncio
from threading import Event

import pytest

from keystone.core.asyncio import run
from keystone.platform.files.adapters.storage import initialize


def test_local_lease_is_cross_process_and_native_io_finishes_before_cancellation(tmp_path):
    import subprocess
    import sys

    from keystone.core.local_leases import LocalLeases
    from keystone.platform.files.adapters.storage import blocking

    initialize(tmp_path / "store")
    storage = LocalLeases(tmp_path / "store")
    with storage.lease("one"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from keystone.core.local_leases import LocalLeases; import sys; "
                'LocalLeases(sys.argv[1]).lease("one").__enter__()',
                str(storage.root),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode != 0 and b"file" in result.stderr.lower()
    with storage.lease("one"):
        pass

    async def cancel_io():
        started, finish = Event(), Event()

        def native():
            started.set()
            assert finish.wait(5)

        task = asyncio.create_task(blocking(native))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel_io())
