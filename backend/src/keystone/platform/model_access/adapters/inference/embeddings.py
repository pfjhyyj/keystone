"""OpenAI-compatible text embeddings, through the existing controlled SDK transport."""

import json
from dataclasses import dataclass

from keystone.platform.model_access.schemas.budgets import ModelRequestBudget
from keystone.platform.model_access.schemas.embeddings import EmbeddingRequest, validate_vectors
from keystone.platform.model_access.schemas.errors import AdapterError


@dataclass(frozen=True)
class Embeddings:
    operations: frozenset = frozenset({"text.embed"})
    delivery = "sync"
    models = None
    paths = {"/embeddings": "POST"}
    timeout_seconds = 60

    def request_target(self, selected):
        return selected.api_base, "/embeddings", "model"

    def validate_request(self, request, selected, target):
        if len(request.content) > 262144:
            raise AdapterError(413, "model_input_too_large", "向量请求超过上限")
        try:
            value = json.loads(request.content)
        except (ValueError, RecursionError):
            raise AdapterError(422, "model_invalid_request", "向量请求不是有效 JSON 对象") from None
        if not isinstance(value, dict):
            raise AdapterError(422, "model_invalid_request", "向量请求不是有效 JSON 对象")
        if (
            request.method != "POST"
            or str(request.url) != target[0] + target[1]
            or value.get("model") != selected.provider_model
        ):
            raise AdapterError(409, "model_target_changed", "向量请求目标发生变化")
        if (
            set(value) != {"model", "input", "dimensions", "encoding_format"}
            or value["encoding_format"] != "float"
        ):
            raise AdapterError(422, "model_invalid_request", "向量请求包含未允许的字段或编码")
        try:
            EmbeddingRequest(texts=value["input"], dimensions=value["dimensions"])
        except (TypeError, ValueError):
            raise AdapterError(422, "model_invalid_request", "向量文本或维度无效") from None
        return ModelRequestBudget(operation="text.embed", max_output_tokens=0)

    async def invoke(self, sdk, selected, data):
        reply = await sdk.embeddings.with_raw_response.create(
            model=selected.provider_model,
            input=data.texts,
            dimensions=data.dimensions,
            encoding_format="float",
        )
        try:
            # Validate wire types before the SDK can coerce bools/numeric strings.
            payload = json.loads(reply.content)
            if not isinstance(payload, dict) or payload.get("model") != selected.provider_model:
                raise ValueError("Embedding model changed")
            return validate_vectors(payload.get("data"), len(data.texts), data.dimensions)
        except (TypeError, ValueError, AttributeError, RecursionError):
            raise AdapterError(
                502, "model_response_unknown", "向量回复模型、顺序或维度无效，禁止自动重发"
            ) from None
