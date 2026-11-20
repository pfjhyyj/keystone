"""Content reads authorize each request against the current business relationship."""

from uuid import UUID

import pytest
from test_files_dictionary import client as client
from test_files_dictionary import upload

pytestmark = pytest.mark.postgres


def test_each_new_read_rechecks_the_business_relationship(client):
    from sqlalchemy import text
    from test_identity import query

    file = upload(client).json()
    path = f"/api/v1/_test/materials/{file['id']}/content"
    assert client.get(path).status_code == 200
    query(
        client,
        lambda session: session.execute(
            text("DELETE FROM keystone.test_materials WHERE id=:id"), {"id": UUID(file["id"])}
        ),
    )
    assert client.get(path).status_code == 404
