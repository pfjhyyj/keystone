"""Cloud channel wire contracts through the real SDK and controlled transport."""

import json
from uuid import uuid4

import httpx2
import pytest
from support.model_stub import image_bytes

from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.schemas.embeddings import EmbeddingRequest
from keystone.platform.model_access.schemas.images import VisionRequest
from keystone.platform.model_access.schemas.invocations import Invoke, Selection


@pytest.mark.parametrize(
    "provider,operation",
    [
        ("aliyun_bailian", "text.generate"),
        ("volcengine", "text.generate"),
        ("aliyun_bailian", "image.understand"),
        ("volcengine", "image.understand"),
        ("aliyun_bailian", "text.embed"),
    ],
)
async def test_cloud_wire_keeps_usage_registration_and_bounded_requests(provider, operation):
    adapter = CHANNELS[provider]
    sender = InferenceClient(http := HttpClient())
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider=provider,
        api_base=adapter.api_base,
        provider_model="model-id",
        operation=operation,
    )
    sent, registrations, receipts = [], [], []

    async def before_send():
        registrations.append(True)
        return "inference-secret"

    async def receipt():
        receipts.append(True)

    def respond(request):
        assert registrations == [True]
        assert request.headers["authorization"] == "Bearer inference-secret"
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "model-id"
        if operation == "text.embed":
            assert str(request.url) == adapter.api_base + "/embeddings"
            assert body == {
                "model": "model-id",
                "input": ["hello"],
                "dimensions": 2,
                "encoding_format": "float",
            }
            return httpx2.Response(
                200,
                headers={"x-request-id": "header-id"},
                json={
                    "model": "model-id",
                    "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
        assert str(request.url) == adapter.api_base + "/chat/completions"
        assert body["max_tokens"] == 1024
        if provider == "aliyun_bailian":
            assert body["enable_thinking"] is False
        if operation == "image.understand":
            image = body["messages"][-1]["content"][0]
            assert image["type"] == "image_url"
            assert image["image_url"]["url"].startswith("data:image/png;base64,")
            return httpx2.Response(
                200,
                headers={"x-request-id": "header-id"},
                json={
                    "id": "vision-id",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "seen"},
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        chunks = [
            {
                "id": "stream-id",
                "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
            },
            {"id": "stream-id", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "id": "stream-id",
                "choices": [],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        ]
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "header-id"},
            content=content + "data: [DONE]\n\n",
        )

    http.transport = lambda _: httpx2.MockTransport(respond)
    observation = Observation(lambda _: None)
    identifier = uuid4()
    if operation == "text.generate":
        events = [
            event
            async for event in sender.events(
                provider,
                selected,
                observation,
                before_send,
                receipt,
                identifier,
                data=Invoke(
                    model_key="model", messages=[{"role": "user", "content": "hello"}], stream=True
                ),
            )
        ]
        assert [e for e in events if e["type"] == "delta"] == [{"type": "delta", "text": "hello"}]
        assert events[-1] == {"type": "result", "content": None}
    else:
        async with sender.connect(
            provider, selected, observation, before_send, receipt, identifier
        ) as sdk:
            if operation == "image.understand":
                data = VisionRequest(
                    messages=[{"role": "user", "content": "describe"}],
                    images=[{"file_id": uuid4()}],
                )
                result = await sender.vision(
                    sdk, selected, data, [("image/png", image_bytes(), None)]
                )
                assert result == "seen"
            else:
                data = EmbeddingRequest(texts=["hello"], dimensions=2)
                assert await sender.embedding(sdk, selected, data) == [[1.0, 0.0]]
    assert len(sent) == len(registrations) == len(receipts) == 1
    assert observation.usage["input_tokens"] == 3
    assert observation.request_budget.operation == operation


@pytest.mark.parametrize("provider", ["aliyun_bailian", "volcengine"])
def test_cloud_credentials_cannot_be_sent_to_other_targets(provider):
    from keystone.core.errors import ServiceError

    sender = InferenceClient(None)
    base = CHANNELS[provider].api_base
    for target, path in [
        (base + "/other", "/chat/completions"),
        ("https://evil.invalid", "/chat/completions"),
        (base, "/images/generations"),
    ]:
        with pytest.raises(ServiceError) as error:
            sender.check_target(provider, target, path)
        assert error.value.code == "model_target_not_allowed"
