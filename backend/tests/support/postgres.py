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

ProbeState = Literal["configured", "ok", "missing", "invalid", "unreachable"]


@dataclass(frozen=True)
class TestDatabaseProbe:
    state: ProbeState
    detail: str
    url: URL | None = None

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
    value: str | None = None, *, timeout: int = PROBE_TIMEOUT_SECONDS
) -> TestDatabaseProbe:
    """Connect once with a bounded timeout and run ``SELECT 1``."""
    parsed = parse_test_database_url(value)
    if parsed.state != "configured":
        return parsed
    assert parsed.url is not None
    url = parsed.url
    maintenance_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        with psycopg.connect(
            maintenance_url,
            autocommit=True,
            connect_timeout=max(1, int(timeout)),
        ) as connection:
            connection.execute("SELECT 1")
    except (psycopg.Error, OSError) as error:
        detail = str(error)
        if url.password:
            detail = re.sub(rf":{re.escape(url.password)}@", ":***@", detail)
        return TestDatabaseProbe("unreachable", detail, url)
    safe_url = url.render_as_string(hide_password=True)
    return TestDatabaseProbe("ok", f"connected to {safe_url}", url)
