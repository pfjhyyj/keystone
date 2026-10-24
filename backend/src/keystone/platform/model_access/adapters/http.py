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

    async def public_address(self, hostname):
        import ipaddress
        import socket

        from keystone.core.asyncio import blocking
        from keystone.core.errors import ServiceError

        addresses = await blocking(socket.getaddrinfo, hostname, 443, 0, socket.SOCK_STREAM)
        values = list(dict.fromkeys(item[4][0] for item in addresses))
        if not values or any(not ipaddress.ip_address(value).is_global for value in values):
            raise ServiceError(502, "model_result_source_denied", "图片来源不是公网地址")
        return values[0]

    async def get_public_bytes(self, provider, url, *, hostname, limit, check_status):
        """Adapter-approved HTTPS host, pinned public address, no credentials/redirects."""
        from keystone.core.errors import ServiceError

        original = httpx2.URL(url)
        if (
            original.scheme != "https"
            or original.host != hostname
            or original.port not in {None, 443}
        ):
            raise ServiceError(502, "model_result_source_denied", "图片来源不受支持")
        address = await self.public_address(hostname)
        target = original.copy_with(host=address)
        async with (
            asyncio.timeout(15),
            httpx2.AsyncClient(
                transport=self.transport(provider),
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            async with client.stream(
                "GET",
                target,
                headers={"Host": hostname},
                extensions={"sni_hostname": hostname},
            ) as response:
                check_status(response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ResponseTooLarge("image_response_too_large")
                return bytes(content)
