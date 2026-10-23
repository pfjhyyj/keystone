"""Psycopg-compatible event loop helpers."""

import asyncio
import sys


def new_event_loop():
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def run(coroutine):
    return asyncio.run(coroutine, loop_factory=new_event_loop)


async def blocking(function, *args, on_cancel=None):
    """Wait for native cleanup before releasing resources, including repeated cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                pass
        if not task.cancelled():
            task.exception()
        raise
