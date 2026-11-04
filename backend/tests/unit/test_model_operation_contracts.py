"""Operation and structured output contracts reject unsafe requests before I/O."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from support.flow_fixture.contracts import definition
from support.flow_packages import snapshot_for

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.definitions import ModelRole
from keystone.platform.flow.schemas.snapshots import read_snapshot
from keystone.platform.model_access.schemas.entries import EntryCreate, EntryEdit
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.invocations import Selection
from keystone.platform.model_access.schemas.structured import (
    JsonOutput,
    StructuredRequest,
    parse_structured,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_operation_contracts_are_explicit_and_selection_requires_provider():
    args = dict(channel_id=uuid4(), provider_model="model")
    assert EntryCreate(key="model", **args).operations == ["text.generate"]
    assert EntryEdit(**args).operations is None
    for operations in (None, [], ["text.generate"] * 2, ["video.generate"]):
        with pytest.raises(ValidationError):
            EntryCreate(key="model", operations=operations, **args)
    snapshot = snapshot_for(definition)
    snapshot["model_bindings"]["review"]["selection"].pop("operation")
    selected = Selection.model_validate(snapshot["model_bindings"]["review"]["selection"])
    assert selected.operation == "text.generate"
    with pytest.raises(ValidationError):
        Selection.model_validate(selected.model_dump(exclude={"provider"}))
    assert (
        read_snapshot(snapshot)["model_bindings"]["review"]["selection"]["operation"]
        == "text.generate"
    )
    snapshot["model_roles"]["review"]["operation"] = "image.generate"
    with pytest.raises(ServiceError, match="运行契约不可用"):
        read_snapshot(snapshot)
    replace(definition, model_roles=(ModelRole("review", "Review", "text.structured"),))


def test_structured_schema_and_result_are_locally_validated():
    output = JsonOutput(name="Answer", json_schema=SCHEMA)
    request = StructuredRequest(messages=[{"role": "user", "content": "Answer"}], output=output)
    assert request.output == output
    assert parse_structured('{"answer": 7}', output) == {"answer": 7}
    for content in (
        '{"answer": "7"}',
        '{"answer": 7, "extra": true}',
        "[]",
        "not json",
        '{"answer": 1, "answer": 2}',
        '{"answer": NaN}',
        "x" * 65537,
    ):
        with pytest.raises(ServiceError) as error:
            parse_structured(content, output)
        assert error.value.code == "model_invalid_output"
    for schema in (
        {"type": "array"},
        {**SCHEMA, "$ref": "https://example.invalid/schema"},
        {**SCHEMA, "$id": "https://example.invalid/"},
        {**SCHEMA, "$dynamicRef": "https://example.invalid/schema"},
        {**SCHEMA, "required": "answer"},
        {**SCHEMA, "description": "x" * 16385},
    ):
        with pytest.raises(ValidationError):
            JsonOutput(name="Answer", json_schema=schema)


def test_image_references_and_real_rasters_are_bounded_and_checked(monkeypatch):
    from io import BytesIO

    from PIL import Image

    from keystone.platform.model_access.adapters.inference import images
    from keystone.platform.model_access.schemas.images import ImageInput, VisionRequest

    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    content = output.getvalue()
    assert images.image_data_uri(content, "image/png").startswith("data:image/png;base64,")
    for body, content_type in (
        (content, "image/jpeg"),
        (content[:20], "image/png"),
        (b"text", "image/png"),
        (b"x" * (4 * 1024 * 1024 + 1), "image/png"),
    ):
        with pytest.raises(AdapterError):
            images.image_data_uri(body, content_type)
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(AdapterError) as error:
        images.image_data_uri(content, "image/png")
    assert error.value.code == "model_invalid_image"
    message = [{"role": "user", "content": "What is in this image?"}]
    image = {"file_id": str(uuid4())}
    VisionRequest(messages=message, images=[image])
    for invalid in (
        [],
        [image] * 5,
        [{"url": "https://example.invalid/image.png"}],
        [{"file_id": "data:image/png;base64,xxx"}],
        [{**image, "detail": "huge"}],
    ):
        with pytest.raises(ValidationError):
            VisionRequest(messages=message, images=invalid)
    assert ImageInput(file_id=image["file_id"]).detail == "auto"


def test_sdk_media_and_native_output_cannot_bypass_declared_role_operation():
    import json

    import httpx2

    from keystone.platform.model_access.adapters.http import HttpClient
    from keystone.platform.model_access.adapters.inference.client import InferenceClient

    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider_model="chat",
        api_base="https://openrouter.ai/api/v1",
        provider="openrouter",
    )
    gateway = InferenceClient(HttpClient())
    base = {"model": "chat", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 1024}
    variants = [
        {
            **base,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.invalid/secret"},
                        }
                    ],
                }
            ],
        },
        {**base, "modalities": ["text", "image"]},
        {**base, "tools": [{"type": "image_generation"}]},
        {
            **base,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "Answer", "schema": SCHEMA, "strict": True},
            },
        },
    ]
    for body in variants:
        request = httpx2.Request(
            "POST", selected.api_base + "/chat/completions", content=json.dumps(body)
        )
        with pytest.raises(ServiceError) as error:
            gateway.validate_request(request, "openrouter", selected)
        assert error.value.code == "model_operation_unavailable"
    request = httpx2.Request(
        "POST", selected.api_base + "/chat/completions", content=json.dumps(base)
    )
    gateway.validate_request(request, "openrouter", selected)


def test_actual_wire_token_allowance_rejects_unbounded_or_multiple_outputs():
    import json

    import httpx2

    from keystone.platform.model_access.adapters.http import HttpClient
    from keystone.platform.model_access.adapters.inference.client import InferenceClient

    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider_model="chat",
        api_base="https://openrouter.ai/api/v1",
        provider="openrouter",
    )
    gateway = InferenceClient(HttpClient())
    base = {"model": "chat", "messages": [{"role": "user", "content": "Hello"}]}

    def validate(values):
        return gateway.validate_request(
            httpx2.Request(
                "POST",
                selected.api_base + "/chat/completions",
                content=json.dumps({**base, **values}),
            ),
            "openrouter",
            selected,
        )

    for values in (
        {},
        {"max_tokens": True},
        {"max_tokens": 4097},
        {"max_tokens": 16, "max_completion_tokens": 16},
        {"max_tokens": 16, "n": 2},
        {"max_tokens": 16, "n": True},
        {"max_tokens": 16, "best_of": 2},
    ):
        with pytest.raises(ServiceError) as error:
            validate(values)
        assert error.value.code == "model_invalid_request"
    for field in ("max_tokens", "max_completion_tokens"):
        assert validate({field: 16}) is None
