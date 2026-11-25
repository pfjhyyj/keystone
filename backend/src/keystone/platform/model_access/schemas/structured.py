"""Bounded object schemas and local validation; never resolve network references."""

import json
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import Field, model_validator

from keystone.core.errors import ServiceError
from keystone.core.schemas import Input
from keystone.platform.model_access.schemas.images import ImageInput
from keystone.platform.model_access.schemas.invocations import MAX_OUTPUT_TOKENS, TextRequest

# A full output allowance at up to 16 bytes per token, as raw text and as the parsed object.
MAX_STRUCTURED_BYTES = MAX_OUTPUT_TOKENS * 16


def bounded_json(value, limit):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Model value must be bounded JSON") from None
    if len(raw) > limit:
        raise ValueError("Model JSON exceeds limit")
    return raw


class JsonOutput(Input):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    json_schema: dict

    @model_validator(mode="after")
    def local_object_schema(self):
        bounded_json(self.json_schema, 16384)
        if self.json_schema.get("type") != "object":
            raise ValueError("Structured output requires an object schema")
        nodes = 0

        def visit(value, depth=0):
            nonlocal nodes
            nodes += 1
            if nodes > 512 or depth > 24:
                raise ValueError("Structured schema is too complex")
            if isinstance(value, dict):
                if "$id" in value or "$dynamicRef" in value:
                    raise ValueError("External schema scopes are unavailable")
                ref = value.get("$ref")
                if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/")):
                    raise ValueError("Only local schema references are supported")
                for child in value.values():
                    visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    visit(child, depth + 1)

        visit(self.json_schema)
        try:
            Draft202012Validator.check_schema(self.json_schema)
        except Exception:
            raise ValueError("Invalid structured output schema") from None
        return self


class StructuredRequest(TextRequest):
    output: JsonOutput
    # Optional authorized File IDs; the model must also declare and support image.understand.
    images: list[ImageInput] = Field(default=[], max_length=4)


class StructuredResult(Input):
    invocation_id: UUID
    content: dict

    @model_validator(mode="after")
    def bounded_content(self):
        bounded_json(self.content, MAX_STRUCTURED_BYTES)
        return self


def parse_structured(content, output):
    try:
        bounded_json(content, MAX_STRUCTURED_BYTES)

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        value = json.loads(content, object_pairs_hook=unique_object)
        bounded_json(value, MAX_STRUCTURED_BYTES)
        Draft202012Validator(output.json_schema, format_checker=FormatChecker()).validate(value)
        return value
    except Exception:
        raise ServiceError(502, "model_invalid_output", "模型输出不符合结构化契约") from None
