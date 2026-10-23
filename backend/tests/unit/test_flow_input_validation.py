"""Flow normalization and input matrices; SQL atomicity remains integration coverage."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ConfigDict, Field, field_serializer
from support.flow_fixture.contracts import definition

from keystone.core.errors import ServiceError
from keystone.core.schemas import Input
from keystone.platform.files.services.references import FileReferences
from keystone.platform.flow.schemas import Start
from keystone.platform.flow.schemas.files import file_ids
from keystone.platform.flow.schemas.values import small_json
from keystone.platform.flow.services.runs import FlowService


async def test_final_input_size_checks_defaults_and_serializers_before_persistence(monkeypatch):
    # The small submitted payload expands only inside the real service's normalization.
    async def check(expansion, extra_bytes):
        padding = ""

        class Params(Input):
            text: str = Field(default_factory=lambda: padding)
            model_key: str = "chat"

            @field_serializer("text")
            def serialize_text(self, value) -> str:
                return padding if expansion == "serializer" and value == "expand" else value

        remaining = 65536 + extra_bytes - len(small_json({"text": "", "model_key": "chat"}))
        padding = "界" * (remaining // 3) + "x" * (remaining % 3)
        flow = replace(
            definition,
            input_type=Params,
            file_fields=(),
            model_field=None,
            required_permissions=(),
        )
        session = SimpleNamespace(
            execute=AsyncMock(),
            scalar=AsyncMock(return_value=None),
            add=Mock(),
            flush=AsyncMock(),
        )
        auth = SimpleNamespace(kind="session", user=SimpleNamespace(id=uuid4()), require=Mock())
        identity = SimpleNamespace(
            settings=SimpleNamespace(flow_enabled=True), correlation_id="unit", log=AsyncMock()
        )
        references = AsyncMock()
        monkeypatch.setattr(FileReferences, "replace", references)
        service = FlowService(identity, {flow.key: flow}, execution_version=lambda: "test")
        data = Start(
            definition_key=flow.key,
            request_key="size",
            input={} if expansion == "default" else {"text": "expand"},
        )
        assert len(small_json(data.input)) < 100
        if extra_bytes:
            with pytest.raises(ServiceError) as error:
                await service.start_in_transaction(session, auth, data)
            assert error.value.status == 422 and error.value.code == "flow_value_too_large"
            session.add.assert_not_called()
            session.flush.assert_not_awaited()
            references.assert_not_awaited()
            identity.log.assert_not_awaited()
        else:
            result = await service.start_in_transaction(session, auth, data)
            assert result["input"] == {"text": padding, "model_key": "chat"}
            assert len(small_json(result["input"])) == 65536
            session.add.assert_called_once()
            session.flush.assert_awaited_once()
            references.assert_awaited_once()
            identity.log.assert_awaited_once()

    for expansion in ("default", "serializer"):
        for extra_bytes in (0, 1):
            await check(expansion, extra_bytes)


def test_file_input_validation_uses_normalized_aliases_and_bounds_unique_ids():
    class FilesInput(Input):
        model_config = ConfigDict(serialize_by_alias=True)
        files: list[str] = Field(alias="fileIds")

    identifiers = [str(uuid4()) for _ in range(101)]
    for invalid in (["bad-id"], identifiers):
        normalized = FilesInput.model_validate({"fileIds": invalid}).model_dump(
            mode="json", by_alias=False
        )
        with pytest.raises(ValueError):
            file_ids(normalized, ("files",))
    for valid in ([], identifiers[:100], identifiers[:100] + identifiers[:1]):
        normalized = FilesInput.model_validate({"fileIds": valid}).model_dump(
            mode="json", by_alias=False
        )
        assert {str(value) for value in file_ids(normalized, ("files",))} == set(valid)
