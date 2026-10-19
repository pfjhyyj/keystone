import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx2
from sqlalchemy import text

from keystone.core.errors import ServiceError
from keystone.platform.model_access import mock

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
LIVE_TARGETS = {
    "openrouter": (
        OPENROUTER_BASE,
        {"/chat/completions": "POST", "/credits": "GET", "/generation": "GET"},
    )
}


class Gateway:
    def __init__(self, settings):
        self.settings = settings
        self.slots = asyncio.Semaphore(4)
        # A refresh holds a coordinator connection and needs another for short writes.
        self.balance_slots = asyncio.Semaphore(2)

    def transport(self, provider):
        self.check_mode(provider)
        if self.settings.model_mode == "mock":
            return mock.transport(provider)
        # Same explicit network boundary as Ether: no transport retries or env proxy.
        return httpx2.AsyncHTTPTransport(retries=0, trust_env=False)

    def check_mode(self, provider):
        if self.settings.model_mode == "disabled":
            raise ServiceError(503, "model_transport_disabled", "模型发送已停用")
        if self.settings.model_mode == "live" and provider not in LIVE_TARGETS:
            raise ServiceError(503, "model_provider_unavailable", "当前真实发送仅支持 OpenRouter")

    def check_target(self, provider, base, path, *, method="POST"):
        self.check_mode(provider)
        if self.settings.model_mode == "live":
            allowed_base, paths = LIVE_TARGETS[provider]
            if base != allowed_base or paths.get(path) != method:
                raise ServiceError(
                    409, "model_target_not_allowed", "真实渠道须使用受支持的官方地址、路径和方法"
                )

    def secret(self, reference):
        value = self.settings.model_secrets.get(reference)
        if value is None or not value.get_secret_value():
            raise ServiceError(503, "model_credential_unavailable", "渠道凭据未配置或已撤销")
        return value.get_secret_value()

    async def query(self, provider, base, path, secret, *, params=None):
        self.check_target(provider, base, path, method="GET")
        async with (
            self.slots,
            asyncio.timeout(10),
            httpx2.AsyncClient(
                transport=self.transport(provider),
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            async with client.stream(
                "GET", base + path, params=params, headers={"Authorization": "Bearer " + secret}
            ) as response:
                if response.status_code != 200:
                    raise ServiceError(502, "model_query_failed", "渠道查询失败")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise ServiceError(502, "model_query_failed", "渠道查询响应超过上限")
                return json.loads(content, parse_float=Decimal)


@asynccontextmanager
async def refresh_lock(database, identifier):
    """Session advisory lock spans external I/O, without an open business transaction."""
    key = int.from_bytes(identifier.bytes[:8], signed=True)
    async with database.engine.connect() as connection:
        acquired = False
        try:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if not acquired:
                raise ServiceError(409, "model_refresh_busy", "该渠道正在刷新，请稍后重试")
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()

            async def still_owned():
                if (
                    connection.invalidated
                    or await connection.scalar(text("SELECT pg_backend_pid()")) != pid
                ):
                    raise ServiceError(503, "model_refresh_interrupted", "刷新协调已中断，请重试")
                await connection.commit()

            yield still_owned
        finally:
            # Never return a session lock to the pool, including cancellation during acquisition.
            # Balance refresh is infrequent; discard this one physical connection on exit.
            await connection.invalidate()
