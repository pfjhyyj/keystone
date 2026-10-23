import psycopg
import pytest
from support import postgres

URL = "postgresql+psycopg://user:secret@127.0.0.1:1/keystone_test"


def test_missing_url_is_reported_without_connecting(monkeypatch):
    monkeypatch.delenv("KEYSTONE_TEST_DATABASE_URL", raising=False)

    probe = postgres.parse_test_database_url()

    assert probe.state == "missing"
    assert not probe.available


@pytest.mark.parametrize(
    "value",
    [
        "sqlite:///keystone_test",
        "postgresql+psycopg://user:secret@127.0.0.1:1/keystone",
        "not a url",
    ],
)
def test_invalid_url_is_rejected(value):
    probe = postgres.parse_test_database_url(value)

    assert probe.state == "invalid"
    assert not probe.available


def test_probe_connects_with_a_bounded_timeout(monkeypatch):
    seen = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            assert statement == "SELECT 1"

    def connect(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Connection()

    monkeypatch.setattr(postgres.psycopg, "connect", connect)

    probe = postgres.probe_test_database(URL, timeout=2)

    assert probe.state == "ok"
    assert probe.available
    assert seen["connect_timeout"] == 2
    assert "secret" not in probe.detail


def test_unreachable_database_hides_the_password(monkeypatch):
    def connect(url, **kwargs):
        raise psycopg.OperationalError(
            "connection failed: postgresql://user:secret@127.0.0.1:1/keystone_test"
        )

    monkeypatch.setattr(postgres.psycopg, "connect", connect)

    probe = postgres.probe_test_database(URL, timeout=1)

    assert probe.state == "unreachable"
    assert not probe.available
    assert "secret" not in probe.detail


def test_short_password_does_not_corrupt_the_driver_message(monkeypatch):
    def connect(url, **kwargs):
        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(postgres.psycopg, "connect", connect)

    probe = postgres.probe_test_database(
        "postgresql+psycopg://user:p@127.0.0.1:1/keystone_test", timeout=1
    )

    assert probe.state == "unreachable"
    assert probe.detail == "connection timeout expired"
