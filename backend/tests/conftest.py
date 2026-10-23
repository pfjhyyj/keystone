import sys
import warnings
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from keystone.core.asyncio import new_event_loop

ROOT = Path(__file__).parent
for directory in (ROOT, ROOT / "unit", ROOT / "integration"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from support.postgres import (  # noqa: E402
    TestDatabaseProbe,
    parse_test_database_url,
    probe_test_database,
)


def pytest_addoption(parser):
    parser.addoption(
        "--require-postgres",
        action="store_true",
        help="Fail instead of skipping DB integration tests",
    )


def _postgres_error(probe: TestDatabaseProbe) -> str:
    return f"{probe.requirement()} Start the dedicated keystone_test database."


def _postgres_probe(config):
    probe = getattr(config, "_keystone_postgres", None)
    if probe is None or probe.state == "configured":
        workers = getattr(config, "workerinput", {}).get("workercount")
        if workers is None:
            workers = len(config.getoption("tx", default=[])) or 1
        probe = probe_test_database(workers=workers)
        config._keystone_postgres = probe
    return probe


def pytest_configure(config):
    parsed = parse_test_database_url()
    config._keystone_postgres_parsed = parsed
    config._keystone_postgres = None
    if not config.getoption("--require-postgres"):
        return
    if parsed.state != "configured":
        raise pytest.UsageError(_postgres_error(parsed))
    probe = _postgres_probe(config)
    if not probe.available:
        raise pytest.UsageError(_postgres_error(probe))


def pytest_collection_finish(session):
    postgres_items = [item for item in session.items if item.get_closest_marker("postgres")]
    if not postgres_items:
        return
    config = session.config
    parsed = getattr(config, "_keystone_postgres_parsed", None) or parse_test_database_url()
    if parsed.state == "invalid":
        raise pytest.UsageError(_postgres_error(parsed))
    probe = _postgres_probe(config)
    if probe.available:
        return
    if probe.state == "insufficient_capacity":
        raise pytest.UsageError(_postgres_error(probe))
    if config.getoption("--require-postgres"):
        raise pytest.UsageError(_postgres_error(probe))
    reason = _postgres_error(probe)
    for item in postgres_items:
        item.add_marker(pytest.mark.skip(reason=reason))


def _database_problem(config, message):
    if config.getoption("--require-postgres"):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


@pytest.fixture
def empty_database_url(pytestconfig):
    """Create and remove only this test's randomly named database."""
    probe = _postgres_probe(pytestconfig)
    if not probe.available:
        _database_problem(pytestconfig, _postgres_error(probe))
    url = probe.url
    assert url is not None
    name = f"keystone_test_{uuid4().hex}"
    connection_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        connection = psycopg.connect(connection_url, autocommit=True, connect_timeout=5)
    except (psycopg.Error, OSError) as error:
        _database_problem(
            pytestconfig, f"Could not connect to the dedicated test database: {error}"
        )
    with connection:
        try:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except (psycopg.Error, OSError) as error:
            _database_problem(pytestconfig, f"Could not create an isolated test database: {error}")
        try:
            yield url.set(database=name).render_as_string(hide_password=False)
        finally:
            try:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                )
            except (psycopg.Error, OSError) as error:
                warnings.warn(
                    f"Could not drop isolated test database {name}: {error}",
                    stacklevel=2,
                )


def pytest_asyncio_loop_factories():
    return {"keystone": new_event_loop}
