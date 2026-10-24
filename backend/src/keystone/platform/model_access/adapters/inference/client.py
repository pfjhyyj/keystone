"""Keystone host: controlled SDK lifetime, send registration and protocol dispatch."""

import asyncio
from contextlib import aclosing, asynccontextmanager
from functools import partial

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from keystone.core.asyncio import blocking
from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.channels import OPENROUTER_BASE as OPENROUTER_BASE
from keystone.platform.model_access.adapters.errors import adapter_errors
from keystone.platform.model_access.adapters.inference.images import verify_image
from keystone.platform.model_access.adapters.inference.protocols import validate_request
from keystone.platform.model_access.adapters.inference.transport import SendTransport


class InferenceClient:
    def __init__(self, http, channels=None):
        self.http = http
        self.channels = channels if channels is not None else CHANNELS
        self.slots = asyncio.Semaphore(4)

    def adapter(self, provider):
        adapter = self.channels.get(provider)
        if adapter is None or not adapter.protocols:
            raise ServiceError(503, "model_provider_unavailable", "当前服务商尚未开放发送")
        return adapter

    def check_operation(self, provider, operation, delivery="sync", *, model=None):
        protocol = self.adapter(provider).protocol(operation, delivery, model=model)
        if protocol is None:
            raise ServiceError(409, "model_operation_unavailable", "协议不支持所需操作")
        return protocol

    def protocol(self, selected, *, provider=None):
        return self.check_operation(
            provider if provider is not None else selected.provider,
            selected.operation,
            selected.delivery,
            model=selected.provider_model,
        )

    def check_target(self, provider, base, path, *, method="POST"):
        if not self.adapter(provider).allows_target(base, path, method):
            raise ServiceError(
                409, "model_target_not_allowed", "渠道须使用受支持的官方地址、路径和方法"
            )

    def transport(self, provider):
        self.adapter(provider)
        return self.http.transport(provider)

    def request_target(self, provider, selected):
        if selected.provider is not None and provider != selected.provider:
            raise ServiceError(409, "model_target_changed", "模型请求目标发生变化")
        protocol = self.protocol(selected, provider=provider)
        target = protocol.request_target(selected)
        self.check_target(provider, selected.api_base, target[1])
        return target

    def validate_request(self, request, provider, selected):
        target = self.request_target(provider, selected)
        with adapter_errors():
            return validate_request(
                request,
                selected,
                target,
                require_parameters=self.protocol(selected, provider=provider).require_parameters,
            )

    @asynccontextmanager
    async def connect(self, provider, selected, observation, before_send, on_receipt, identifier):
        base, _, _ = self.request_target(provider, selected)
        transport = SendTransport(
            self.transport(provider),
            before_send,
            observation,
            on_receipt,
            lambda request: self.validate_request(request, provider, selected),
            identifier,
            authorize=self.adapter(provider).authorize,
        )
        async with (
            self.slots,
            asyncio.timeout(60),
            DefaultAsyncHttpxClient(
                transport=transport, timeout=30, follow_redirects=False, trust_env=False
            ) as http,
            AsyncOpenAI(
                api_key="runtime-resolved",
                base_url=base,
                http_client=http,
                max_retries=0,
                timeout=30,
            ) as sdk,
        ):
            yield sdk

    async def events(
        self,
        provider,
        selected,
        observation,
        before_send,
        on_receipt,
        identifier,
        *,
        data=None,
        sdk_request=None,
    ):
        protocol = self.protocol(selected, provider=provider)
        async with self.connect(
            provider, selected, observation, before_send, on_receipt, identifier
        ) as sdk:
            # Trusted SDK callbacks still pass through the same physical send boundary.
            if sdk_request is not None:
                with adapter_errors():
                    content = await sdk_request(sdk)
                yield {"type": "result", "content": content}
            else:
                with adapter_errors():
                    async with aclosing(
                        protocol.events(sdk, selected, observation, identifier, data=data)
                    ) as events:
                        async for event in events:
                            yield event

    async def image(self, sdk, selected, data, images, identifier, digest):
        with adapter_errors():
            return await self.protocol(selected).invoke(
                sdk, selected, data, images, identifier, digest, blocking
            )

    async def vision(self, sdk, selected, data, images):
        with adapter_errors():
            return await self.protocol(selected).vision(sdk, selected, data, images, blocking)

    def sdk_settings(self, selected, settings):
        if selected.provider is None:
            # Legacy snapshots did not select provider-specific Agent settings.
            return settings
        with adapter_errors():
            return self.protocol(selected).sdk_settings(selected.operation, settings)

    def task_protocol(self, selected):
        protocol = self.protocol(selected)
        if selected.delivery != "async":
            raise ServiceError(409, "model_operation_unavailable", "原生任务协议不受支持")
        self.request_target(selected.provider, selected)
        return protocol

    async def submit_task(self, sdk, selected, data):
        with adapter_errors():
            return await self.task_protocol(selected).submit(sdk, selected, data)

    async def query_task(self, selected, receipt, secret):
        with adapter_errors():
            protocol = self.task_protocol(selected)
            self.check_target(
                selected.provider, selected.api_base, protocol.query_path, method="GET"
            )
            return await protocol.query(self.http, receipt, secret)

    async def download_task_output(self, selected, url):
        with adapter_errors():
            return await self.task_protocol(selected).download(self.http, url)

    async def verify_output_image(self, content):
        from keystone.platform.model_access.schemas.images import MAX_OUTPUT_IMAGE_BYTES

        with adapter_errors():
            return await blocking(
                partial(verify_image, content, None, max_bytes=MAX_OUTPUT_IMAGE_BYTES)
            )
