"""Psycopg-compatible event loops, reused from ether.core.asyncio."""

import asyncio
import sys


def new_event_loop():
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def run(coroutine):
    return asyncio.run(coroutine, loop_factory=new_event_loop)
