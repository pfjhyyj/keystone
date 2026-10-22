"""Bounded HTTP mechanics only; adapters own targets, auth and response semantics."""

import asyncio
import json
from decimal import Decimal

import httpx2


class ResponseTooLarge(ValueError):
    pass


class HttpClient:
    def transport(self, provider):
        return httpx2.AsyncHTTPTransport(retries=0, trust_env=False)

    async def get_json(
        self, provider, base, path, *, headers=None, params=None, limit=65536, check_status
    ):
        async with (
            asyncio.timeout(10),
            httpx2.AsyncClient(
                transport=self.transport(provider),
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            async with client.stream(
                "GET", base + path, headers=headers, params=params
            ) as response:
                check_status(response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ResponseTooLarge("response_too_large")
                return json.loads(content, parse_float=Decimal)
