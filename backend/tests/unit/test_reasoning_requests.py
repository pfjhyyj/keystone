"""Thinking intent reaches real SDK bytes before registration and metering."""

import json
from uuid import uuid4

import httpx2
import pytest
from support.model_stub import image_bytes

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.channels import CHANNELS
from keystone.platform.model_access.schemas.images import VisionRequest
from keystone.platform.model_access.schemas.invocations import Invoke, ReasoningOptions, Selection
from keystone.platform.model_access.schemas.structured import JsonOutput, StructuredRequest


@pytest.mark.parametrize("operation", ["text.generate", "text.structured", "image.understand"])
async def test_reasoning_reaches_wire_and_usage_is_not_added_twice(operation):
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider="aliyun_bailian",
        provider_model="qwen3.8-max",
        api_base=CHANNELS["aliyun_bailian"].api_base,
        operation=operation,
    )
    intent = ReasoningOptions(mode="on", effort="high")
    http = HttpClient()
    sender = InferenceClient(http)
    registrations = []

    async def register():
        registrations.append(True)
        return "test-secret"

    async def receipt():
        pass

    def respond(request):
        body = json.loads(request.content)
        assert registrations == [True]
        assert body["enable_thinking"] is True and body["reasoning_effort"] == "high"
        assert body["max_tokens"] == 1024
        assert request.extensions["timeout"]["read"] == 120
        return httpx2.Response(
            200,
            json={
                "id": "one",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "{}",
                            "reasoning_content": "not persisted",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "completion_tokens_details": {"reasoning_tokens": 15},
                },
            },
        )

    http.transport = lambda _: httpx2.MockTransport(respond)
    observation = Observation(lambda _: None)
    kwargs = {"messages": [{"role": "user", "content": "review"}], "reasoning": intent}
    if operation == "image.understand":
        data = VisionRequest(**kwargs, images=[{"file_id": uuid4()}])
        async with sender.connect(
            selected.provider, selected, observation, register, receipt, uuid4(), reasoning=intent
        ) as sdk:
            assert (
                await sender.vision(sdk, selected, data, [("image/png", image_bytes(), None)])
                == "{}"
            )
    else:
        data = (
            StructuredRequest(
                **kwargs,
                output=JsonOutput(
                    name="result",
                    json_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
            )
            if operation == "text.structured"
            else Invoke(**kwargs, model_key="model")
        )
        assert [
            event
            async for event in sender.events(
                selected.provider,
                selected,
                observation,
                register,
                receipt,
                uuid4(),
                data=data,
            )
        ][-1]["content"] == "{}"
    assert observation.usage == {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "reasoning_tokens": 15,
    }


async def test_unsupported_intent_or_callback_override_never_registers_or_sends():
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider="aliyun_bailian",
        provider_model="qwen3.8-max",
        api_base=CHANNELS["aliyun_bailian"].api_base,
    )
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(lambda _: pytest.fail("Unapproved send"))
    sender = InferenceClient(http)
    intent = ReasoningOptions(mode="on", effort="high")
    with pytest.raises(ServiceError):
        sender.sdk_settings(
            selected.model_copy(update={"provider_model": "unknown"}), {}, reasoning=intent
        )
    registered = []

    async def register():
        registered.append(True)
        return "secret"

    async def receipt():
        pass

    async with sender.connect(
        selected.provider,
        selected,
        Observation(lambda _: None),
        register,
        receipt,
        uuid4(),
        reasoning=intent,
    ) as sdk:
        with pytest.raises(ServiceError, match="思考设置"):
            await sdk.chat.completions.create(
                model=selected.provider_model,
                messages=[{"role": "user", "content": "test"}],
                max_tokens=10,
                extra_body={"enable_thinking": False},
            )
    assert registered == []
