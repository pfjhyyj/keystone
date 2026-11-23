"""Bounded HTTP calls to Feishu shared by the adapters; bodies are parsed, never logged."""

import asyncio
import json

import httpx2

MAX_RESPONSE = 64 * 1024
TIMEOUT = 10


class Rejected(Exception):
    """The provider answered and did not deliver; safe to report as failed."""

    def __init__(self, code):
        self.code = code


class Unknown(Exception):
    """No trustworthy answer; the message may or may not have been delivered."""


async def call(method, url, *, transport=None, **request):
    """Return (status, parsed JSON or None); transport failures and oversize answers are Unknown."""
    try:
        async with (
            asyncio.timeout(TIMEOUT),
            httpx2.AsyncClient(
                transport=transport or httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
                timeout=TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            async with client.stream(method, url, **request) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise Unknown
                status = response.status_code
    except (httpx2.HTTPError, OSError, TimeoutError):
        raise Unknown from None
    try:
        answer = json.loads(data)
    except ValueError:
        answer = None
    return status, answer if isinstance(answer, dict) else None
