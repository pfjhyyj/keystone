import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from keystone.core.asyncio import new_event_loop


def pytest_asyncio_loop_factories():
    return {"keystone": new_event_loop}


@pytest.fixture
def empty_database_url():
    """Create and remove only this test's randomly named database."""
    value = os.environ.get("KEYSTONE_TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set KEYSTONE_TEST_DATABASE_URL to a dedicated keystone_test database")
    url = make_url(value)
    if url.database != "keystone_test":
        pytest.fail("Integration tests require a dedicated database named keystone_test")
    name = f"keystone_test_{uuid4().hex}"
    connection_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(connection_url, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield url.set(database=name).render_as_string(hide_password=False)
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
