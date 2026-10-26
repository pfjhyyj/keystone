"""Bounded text-only embedding values; never accept arbitrary upstream bodies."""

import math
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from keystone.core.schemas import Input


class EmbeddingRequest(Input):
    texts: list[str] = Field(min_length=1, max_length=16)
    dimensions: int = Field(ge=1, le=4096, strict=True)

    @model_validator(mode="after")
    def bounded(self):
        if any(not text.strip() or len(text.encode()) > 8192 for text in self.texts):
            raise ValueError("Each embedding text must be nonempty and at most 8 KiB")
        if sum(len(text.encode()) for text in self.texts) > 32768:
            raise ValueError("Embedding input exceeds 32 KiB")
        return self


class EmbeddingResult(Input):
    invocation_id: UUID
    vectors: list[
        Annotated[
            list[Annotated[float, Field(strict=True, allow_inf_nan=False)]],
            Field(min_length=1, max_length=4096),
        ]
    ] = Field(min_length=1, max_length=16)


def validate_vectors(values, count, dimensions):
    if (
        not isinstance(values, list)
        or len(values) != count
        or any(not isinstance(v, dict) or type(v.get("index")) is not int for v in values)
        or sorted(v["index"] for v in values) != list(range(count))
    ):
        raise ValueError("Embedding response does not correspond to its inputs")
    result = []
    for value in sorted(values, key=lambda v: v["index"]):
        vector = value.get("embedding")
        if (
            not isinstance(vector, list)
            or len(vector) != dimensions
            or any(
                type(v) not in (int, float) or abs(v) > 3.4e38 or not math.isfinite(v)
                for v in vector
            )
            or not any(v != 0 for v in vector)
        ):
            raise ValueError("Invalid embedding vector")
        result.append(vector)
    return result
