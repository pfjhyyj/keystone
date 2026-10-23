"""Validate the whole reference batch before any lock, release or audit I/O."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from keystone.platform.files.services.references import FileReferenceService


async def test_invalid_claims_are_rejected_before_any_release():
    file_id = uuid4()
    valid = ("orders:one", "result", file_id)
    for owner, slot, identifier in (
        ("", "slot", file_id),
        ("o" * 161, "slot", file_id),
        ("o", "s" * 101, file_id),
        ("o", "slot", "bad-id"),
    ):
        session = SimpleNamespace(execute=AsyncMock())
        identity = SimpleNamespace(log=AsyncMock())
        with pytest.raises(ValueError):
            await FileReferenceService(identity).release_in_transaction(
                session, None, [valid, (owner, slot, identifier)]
            )
        session.execute.assert_not_awaited()
        identity.log.assert_not_awaited()
