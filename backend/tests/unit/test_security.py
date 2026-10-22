from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from keystone.composition import PLATFORM_CATALOG
from keystone.core.catalog import Catalog
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor, record, validate_details
from keystone.platform.identity.adapters.security import csrf_token
from keystone.platform.identity.schemas import PasswordChange, PasswordReset, UserCreate
from keystone.platform.identity.services.authorization import permission_catalog
from keystone.platform.identity.services.login_limits import LoginLimiter


def test_database_rejects_missing_application_catalog_before_creating_engine(monkeypatch):
    create_engine = Mock()
    monkeypatch.setattr("keystone.core.database.create_async_engine", create_engine)
    settings = Settings(
        config_file=None, database_url="postgresql+psycopg://user:password@localhost/keystone"
    )
    with pytest.raises(TypeError):
        Database(settings)
    with pytest.raises(TypeError, match="explicit Catalog"):
        Database(settings, catalog=None)
    create_engine.assert_not_called()


@pytest.mark.parametrize("info", [{}, {"catalog": Catalog()}])
async def test_missing_or_empty_catalog_cannot_inherit_platform_authority(info):
    session = SimpleNamespace(info=info, add=Mock(), flush=AsyncMock())
    if info:
        assert not permission_catalog(session)
    else:
        with pytest.raises(KeyError, match="catalog"):
            permission_catalog(session)
    with pytest.raises(KeyError):
        await record(session, "identity.login", Actor("system"))
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


def test_new_password_length_without_complexity_requirements():
    # UserCreate is also used by the administrator initialization command.
    for schema, extra in (
        (UserCreate, {"username": "alice", "display_name": "Alice"}),
        (PasswordReset, {}),
        (PasswordChange, {"current_password": "old-password"}),
    ):
        for password in ("12345678", "aaaaaaaa", " abcdef ", "x" * 128):
            assert schema(password=password, **extra).password.get_secret_value() == password
        for password in ("1234567", "x" * 129):
            with pytest.raises(ValidationError):
                schema(password=password, **extra)


def test_production_requires_https_origins_and_csrf_is_session_bound():
    with pytest.raises(ValidationError):
        Settings(config_file=None, env="production", browser_origins=["http://example.test"])
    assert csrf_token("first-session-secret") != csrf_token("second-session-secret")
    assert csrf_token("first-session-secret") != "first-session-secret"


def test_audit_allowlist_rejects_nested_secret_fields():
    with pytest.raises(ValueError):
        validate_details(
            {"after": {"password_hash": "must-not-persist"}},
            PLATFORM_CATALOG.audit_actions["identity.user.update"],
        )


def test_login_limiter_expires_without_unbounded_username_growth(monkeypatch):
    monkeypatch.setattr(
        "keystone.platform.identity.services.login_limits.time.monotonic", lambda: 100
    )
    limiter = LoginLimiter()
    for _ in range(5):
        limiter.check("alice")
    with pytest.raises(ServiceError):
        limiter.check("alice")
    for index in range(55):
        limiter.check(f"name-{index}")
    with pytest.raises(ServiceError):
        limiter.check("another-new-name")
    assert len(limiter.buckets) <= 61
    monkeypatch.setattr(
        "keystone.platform.identity.services.login_limits.time.monotonic", lambda: 161
    )
    limiter.check("alice")
    assert len(limiter.buckets) == 2
