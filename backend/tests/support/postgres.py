"""Read-only probes for the dedicated PostgreSQL test database."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

import psycopg
from sqlalchemy.engine import URL, make_url

TEST_DATABASE_NAME = "keystone_test"
TEST_DRIVERNAME = "postgresql+psycopg"
PROBE_TIMEOUT_SECONDS = 3
# API pool (10), DBOS client (5), two Workers (15 each), and transient connections.
CONNECTIONS_PER_WORKER = 48
CONNECTION_RESERVE = 16

ProbeState = Literal[
    "configured", "ok", "missing", "invalid", "unreachable", "insufficient_capacity"
]


@dataclass(frozen=True)
class TestDatabaseProbe:
    state: ProbeState
    detail: str
    url: URL | None = None
    connection_capacity: int | None = None

    @property
    def available(self) -> bool:
        return self.state == "ok"

    def requirement(self) -> str:
        return f"PostgreSQL test database is unavailable ({self.state}): {self.detail}."


def parse_test_database_url(value: str | None = None) -> TestDatabaseProbe:
    """Validate the URL shape without opening a connection."""
    if value is None:
        value = os.environ.get("KEYSTONE_TEST_DATABASE_URL")
    if not value:
        return TestDatabaseProbe("missing", "KEYSTONE_TEST_DATABASE_URL is not set")
    try:
        url = make_url(value)
    except Exception:
        return TestDatabaseProbe(
            "invalid", "KEYSTONE_TEST_DATABASE_URL is not a valid SQLAlchemy URL"
        )
    if url.drivername != TEST_DRIVERNAME:
        return TestDatabaseProbe(
            "invalid",
            f"KEYSTONE_TEST_DATABASE_URL uses {url.drivername!r}; expected {TEST_DRIVERNAME!r}",
        )
    if url.database != TEST_DATABASE_NAME:
        return TestDatabaseProbe(
            "invalid",
            f"KEYSTONE_TEST_DATABASE_URL targets {url.database!r}; expected {TEST_DATABASE_NAME!r}",
        )
    return TestDatabaseProbe("configured", "KEYSTONE_TEST_DATABASE_URL is configured", url)


def probe_test_database(
    value: str | None = None, *, timeout: int = PROBE_TIMEOUT_SECONDS, workers: int | None = None
) -> TestDatabaseProbe:
    """Read-only connectivity and optional parallel-test capacity check."""
    parsed = parse_test_database_url(value)
    if parsed.state != "configured":
        return parsed
    assert parsed.url is not None
    url = parsed.url
    maintenance_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    capacity = None
    try:
        with psycopg.connect(
            maintenance_url,
            autocommit=True,
            connect_timeout=max(1, int(timeout)),
        ) as connection:
            connection.execute("SELECT 1")
            if workers is not None:
                capacity = connection.execute(
                    """SELECT current_setting('max_connections')::int
                         - current_setting('superuser_reserved_connections')::int
                         - COALESCE(current_setting('reserved_connections', true), '0')::int"""
                ).fetchone()[0]
    except (psycopg.Error, OSError) as error:
        detail = str(error)
        if url.password:
            detail = re.sub(rf":{re.escape(url.password)}@", ":***@", detail)
        return TestDatabaseProbe("unreachable", detail, url)
    if capacity is not None:
        required = CONNECTIONS_PER_WORKER * max(1, workers) + CONNECTION_RESERVE
        if capacity < required:
            return TestDatabaseProbe(
                "insufficient_capacity",
                f"{capacity} ordinary connections available in server configuration; "
                f"{workers} test workers require {required}. "
                "Use tests/postgres/compose.yaml (port 55433) or provision equivalent capacity",
                url,
                capacity,
            )
    safe_url = url.render_as_string(hide_password=True)
    return TestDatabaseProbe("ok", f"connected to {safe_url}", url, capacity)
