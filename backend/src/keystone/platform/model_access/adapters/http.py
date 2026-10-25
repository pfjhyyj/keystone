"""Bounded HTTP mechanics only; adapters own targets, auth and response semantics."""

import asyncio
import json
from decimal import Decimal

import httpx2

from keystone.platform.model_access.schemas.errors import ResponseTooLarge as ResponseTooLarge


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

    async def get_bytes(self, provider, url, *, hostname, limit, check_status, timeout=None):
        """Fetch from the adapter-approved HTTPS host using normal transport resolution."""
        from keystone.core.errors import ServiceError

        original = httpx2.URL(url)
        if (
            original.scheme != "https"
            or original.host != hostname
            or original.port not in {None, 443}
        ):
            raise ServiceError(502, "model_result_source_denied", "图片来源不受支持")
        async with (
            asyncio.timeout(15 if timeout is None else timeout),
            httpx2.AsyncClient(
                transport=self.transport(provider),
                timeout=10 if timeout is None else timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            async with client.stream(
                "GET",
                original,
            ) as response:
                check_status(response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ResponseTooLarge("image_response_too_large")
                return bytes(content)
