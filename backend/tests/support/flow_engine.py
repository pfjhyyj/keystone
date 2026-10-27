"""Explicit DBOS setup for tests that execute or inspect persisted workflows."""

import pytest


@pytest.fixture
def flow_engine(client, configuration):
    from dbos import DBOS

    from keystone.platform.flow.adapters.dbos import Engine

    runtime = client.app.state.runtime
    DBOS.migrate(runtime.settings.database_url.get_secret_value())
    runtime.flow_engine = Engine(runtime.settings)
