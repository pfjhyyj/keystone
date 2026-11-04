import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from keystone.bootstrap import create_app
from keystone.composition import PLATFORM_CATALOG, build_catalog
from keystone.core.asyncio import new_event_loop, run
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.migrations import migration_config
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.identity.adapters.security import Passwords
from keystone.platform.identity.models import LoginSession, PersonalAccessToken, User
from keystone.platform.identity.schemas import UserCreate
from keystone.platform.identity.services.initialize import initialize_admin

pytestmark = pytest.mark.postgres
PASSWORD = "test-password-123"
NEW_PASSWORD = "changed-password-123"
ORIGIN = "http://127.0.0.1:5173"
BROWSER = {"Origin": ORIGIN, "X-Keystone-Request": "1"}


@pytest.fixture
def client(empty_database_url):
    settings = Settings(config_file=None, database_url=empty_database_url, env="test")
    config = migration_config(settings)
    config.attributes["create_schema"] = True
    command.upgrade(config, "head")

    async def initialize():
        database = Database(settings, catalog=build_catalog())
        try:
            await initialize_admin(
                database,
                Passwords(),
                UserCreate(username="admin", display_name="管理员", password=PASSWORD),
            )
        finally:
            await database.close()

    run(initialize())
    with TestClient(
        create_app(settings), backend_options={"loop_factory": new_event_loop}
    ) as browser:
        yield browser


def login(client, username="admin", password=PASSWORD):
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}, headers=BROWSER
    )
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    client.headers.update({**BROWSER, "X-CSRF-Token": response.json()["csrf_token"]})
    return response.json()["user"]


def create_user(client, name="alice"):
    response = client.post(
        "/api/v1/users", json={"username": name, "display_name": name, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def role(client, key, permissions):
    response = client.post(
        "/api/v1/roles", json={"key": key, "name": key, "permissions": permissions}
    )
    assert response.status_code == 201, response.text
    return response.json()


def assign(client, user_id, role_id, assigned=True):
    return client.put(f"/api/v1/users/{user_id}/roles/{role_id}", json={"assigned": assigned})


def token(client, permissions):
    response = client.post(
        "/api/v1/auth/tokens",
        json={
            "name": "test",
            "permissions": permissions,
            "expires_at": (utcnow() + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def bearer(value):
    return {"Authorization": f"Bearer {value}"}


def query(client, operation):
    async def execute():
        async with client.app.state.runtime.database.transaction() as session:
            return await operation(session)

    return client.portal.call(execute)


@pytest.mark.parametrize("resource", ["users", "roles"])
def test_lists_batch_only_current_page_associations(client, resource):
    from uuid import UUID

    from sqlalchemy import event

    from keystone.platform.identity.models import RolePermission

    login(client)
    alice, bob = create_user(client), create_user(client, "bob")
    empty = role(client, "empty", [])
    reader = role(client, "reader", ["dictionary:read", "audit:read"])
    for value in (empty, reader):
        assert assign(client, alice["id"], value["id"]).status_code == 204

    async def obsolete_permission(session):
        session.add(RolePermission(role_id=UUID(reader["id"]), permission_key="removed:permission"))

    query(client, obsolete_permission)
    table = "user_roles" if resource == "users" else "role_permissions"
    engine = client.app.state.runtime.database.engine.sync_engine

    def page(**params):
        statements, associations = [], []

        def capture(connection, cursor, statement, parameters, context, many):
            statements.append(statement)
            if f"FROM keystone.{table} " in " ".join(statement.split()):
                associations.append(parameters)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = client.get(f"/api/v1/{resource}", params=params)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert response.status_code == 200, response.text
        rows = response.json()
        assert len(associations) == (1 if rows else 0)
        if rows:
            assert {str(value) for value in associations[0].values()} == {row["id"] for row in rows}
        return rows, len(statements)

    rows, count = page(limit=100)
    first, single_count = page(limit=1)
    second, second_count = page(offset=1, limit=1)
    assert count == single_count == second_count
    assert first == rows[:1] and second == rows[1:2]
    assert page(offset=100)[0] == []
    if resource == "users":
        by_id = {row["id"]: row for row in rows}
        assert set(by_id[alice["id"]]["role_ids"]) == {empty["id"], reader["id"]}
        assert by_id[bob["id"]]["role_ids"] == []
        assert page(q="ALICE")[0] == [by_id[alice["id"]]]
        assert page(q="missing")[0] == []
    else:
        by_key = {row["key"]: row for row in rows}
        assert by_key["empty"]["permissions"] == []
        assert by_key["reader"]["permissions"] == ["audit:read", "dictionary:read"]
        assert by_key["admin"]["permissions"] == sorted(PLATFORM_CATALOG.permissions)


def test_batched_role_grants_preserve_account_and_token_rules(client):
    from uuid import UUID

    from sqlalchemy import event

    from keystone.core.errors import ServiceError
    from keystone.platform.identity.models import RolePermission
    from keystone.platform.identity.services.authorization import (
        identity_lock,
        load_authorization,
        load_authorization_for_target,
        role_grants,
    )

    admin = login(client)
    user = create_user(client)
    unassigned = create_user(client, "unassigned")
    roles = [
        role(client, "first", ["dictionary:read", "audit:read"]),
        role(client, "overlap", ["dictionary:read"]),
        role(client, "empty", []),
    ]
    for value in roles:
        assert assign(client, user["id"], value["id"]).status_code == 204

    async def exercise(session):
        await identity_lock(session)
        session.add(
            RolePermission(role_id=UUID(roles[1]["id"]), permission_key="removed:permission")
        )
        await session.flush()
        statements = []
        engine = client.app.state.runtime.database.engine.sync_engine

        def capture(connection, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            grouped, administrator = await role_grants(session, UUID(user["id"]))
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert grouped[UUID(roles[2]["id"])] == set() and not administrator
        expected = {"dictionary:read", "audit:read"}
        assert set().union(*grouped.values()) == expected
        assert await role_grants(session, UUID(unassigned["id"])) == ({}, False)
        admin_roles, administrator = await role_grants(session, UUID(admin["id"]))
        assert administrator and set().union(*admin_roles.values()) == set(
            PLATFORM_CATALOG.permissions
        )

        target = await session.get(User, UUID(user["id"]))
        assert await load_authorization_for_target(session, target) == (expected, False)
        forced = await load_authorization(session, target.id)
        assert forced.permissions == set()
        with pytest.raises(ServiceError) as denied:
            forced.require("dictionary:read")
        assert denied.value.code == "permission_denied"
        target.password_change_required = False
        pat = PersonalAccessToken(
            user_id=target.id,
            name="scope",
            token_digest="0" * 64,
            permissions=["dictionary:read"],
            expires_at=utcnow() + timedelta(days=1),
        )
        session.add(pat)
        await session.flush()
        auth = await load_authorization(session, target.id, pat_id=pat.id)
        assert auth.permissions == {"dictionary:read"}
        assert auth.role_ids == frozenset(UUID(value["id"]) for value in roles)
        assert (await load_authorization(session, UUID(unassigned["id"]))).role_ids == frozenset()
        auth.require("dictionary:read")
        with pytest.raises(ServiceError):
            auth.require("audit:read")
        for current, allowed in (
            (await load_authorization(session, target.id), expected),
            (auth, {"dictionary:read"}),
            (
                await load_authorization(session, UUID(admin["id"])),
                set(PLATFORM_CATALOG.permissions),
            ),
            (await load_authorization(session, UUID(unassigned["id"])), set()),
        ):
            for permission in (*PLATFORM_CATALOG.permissions, "dictionary:*", "removed:permission"):
                if permission in allowed:
                    current.require(permission)
                else:
                    with pytest.raises(ServiceError) as denied:
                        current.require(permission)
                    assert denied.value.status == 403 and denied.value.code == "permission_denied"
        pat.user_id = UUID(admin["id"])
        await session.flush()
        restricted_admin = await load_authorization(session, pat.user_id, pat_id=pat.id)
        assert restricted_admin.administrator
        restricted_admin.require("dictionary:read")
        with pytest.raises(ServiceError):
            restricted_admin.require("user:create")
        target.is_active = False
        await session.flush()
        with pytest.raises(ServiceError):
            await load_authorization(session, target.id)
        assert await load_authorization_for_target(session, target) == (expected, False)

    query(client, exercise)


def test_login_csrf_cookie_and_forced_password_change(client):
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).status_code
        == 403
    )
    admin = login(client)
    assert client.cookies.get("keystone_session").startswith("ks_")
    assert client.get("/api/v1/auth/me").json()["is_administrator"] is True
    user = create_user(client)
    session_secret = client.cookies.get("keystone_session")
    client.headers["X-CSRF-Token"] = "wrong"
    assert client.post("/api/v1/auth/logout").status_code == 403
    client.headers["Origin"] = "https://evil.example"
    assert client.post("/api/v1/auth/logout").status_code == 403
    login(client, "alice")  # Existing cookie must not prevent a new login.
    assert client.get("/api/v1/auth/me").json()["user"]["password_change_required"] is True
    assert client.get("/api/v1/users").json()["code"] == "password_change_required"
    assert (
        client.post(
            "/api/v1/auth/tokens",
            json={
                "name": "blocked",
                "permissions": [],
                "expires_at": (utcnow() + timedelta(days=1)).isoformat(),
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    assert client.cookies.get("keystone_session") is None
    assert client.get("/api/v1/auth/me").status_code == 401
    login(client, "alice", NEW_PASSWORD)
    assert client.get("/api/v1/auth/me").json()["user"]["id"] == user["id"]
    assert client.get("/api/v1/users").status_code == 403
    # A database ID never substitutes for a secret, and bearer never falls back to Cookie.
    assert client.get("/api/v1/auth/me", headers=bearer(admin["id"])).status_code == 401
    assert session_secret not in client.get("/api/v1/auth/me").text


def test_pat_scopes_revocation_and_disable_never_revive_credentials(client):
    admin = login(client)
    limited = token(client, ["user:read"])
    assert client.get("/api/v1/users", headers=bearer(limited["token"])).status_code == 200
    assert client.get("/api/v1/roles", headers=bearer(limited["token"])).status_code == 403
    assert client.get("/api/v1/auth/tokens", headers=bearer(limited["token"])).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/password",
            headers=bearer(limited["token"]),
            json={"current_password": PASSWORD, "password": NEW_PASSWORD},
        ).status_code
        == 403
    )
    listing = client.get("/api/v1/auth/tokens").json()
    assert "token" not in listing[0] and "token_digest" not in listing[0]
    assert client.delete(f"/api/v1/auth/tokens/{limited['id']}").status_code == 204
    assert client.get("/api/v1/users", headers=bearer(limited["token"])).status_code == 401
    # Last admin cannot be disabled, even by an all-scope PAT.
    full = token(client, list(PLATFORM_CATALOG.permissions))
    assert (
        client.put(
            f"/api/v1/users/{admin['id']}/active",
            json={"is_active": False},
            headers=bearer(full["token"]),
        ).json()["code"]
        == "last_administrator"
    )
    user = create_user(client)
    login(client, "alice")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "alice", NEW_PASSWORD)
    personal = token(client, [])
    old_cookie = client.cookies.get("keystone_session")
    login(client)
    assert (
        client.put(f"/api/v1/users/{user['id']}/active", json={"is_active": False}).status_code
        == 200
    )
    assert (
        client.put(f"/api/v1/users/{user['id']}/active", json={"is_active": True}).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me", headers=bearer(personal["token"])).status_code == 401
    client.cookies.set("keystone_session", old_cookie, domain="testserver.local", path="/")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_current_grants_pat_intersection_and_management_boundaries(client):
    admin = login(client)
    user = create_user(client)
    reader = role(client, "reader", ["user:read"])
    assert assign(client, user["id"], reader["id"]).status_code == 204
    admin_cookie = client.cookies.get("keystone_session")
    admin_csrf = client.headers["X-CSRF-Token"]
    login(client, "alice")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "alice", NEW_PASSWORD)
    personal = token(client, ["user:read"])
    assert client.get("/api/v1/users", headers=bearer(personal["token"])).status_code == 200
    client.cookies.set("keystone_session", admin_cookie, domain="testserver.local", path="/")
    client.headers["X-CSRF-Token"] = admin_csrf
    assert (
        client.put(
            f"/api/v1/roles/{reader['id']}", json={"name": "reader", "permissions": []}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/users", headers=bearer(personal["token"])).status_code == 403
    assert (
        client.put(
            f"/api/v1/roles/{reader['id']}", json={"name": "reader", "permissions": ["user:read"]}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/users", headers=bearer(personal["token"])).status_code == 200
    assert client.delete(f"/api/v1/roles/{reader['id']}").json()["code"] == "role_in_use"
    # All ordinary permissions do not grant the administrator's special identity.
    full_role = role(client, "ordinary-full", list(PLATFORM_CATALOG.permissions))
    assert assign(client, user["id"], full_role["id"]).status_code == 204
    admin_role = next(r for r in client.get("/api/v1/roles").json() if r["key"] == "admin")
    login(client, "alice", NEW_PASSWORD)
    assert (
        client.post(
            f"/api/v1/users/{admin['id']}/password", json={"password": NEW_PASSWORD}
        ).status_code
        == 403
    )
    assert assign(client, user["id"], admin_role["id"]).status_code == 403
    assert assign(client, admin["id"], reader["id"]).status_code == 403


def test_reset_revokes_all_credentials_and_requires_change(client):
    login(client)
    user = create_user(client)
    login(client, "alice")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "alice", NEW_PASSWORD)
    personal = token(client, [])
    old_cookie = client.cookies.get("keystone_session")
    login(client, "alice", NEW_PASSWORD)
    second = token(client, [])
    login(client)
    operator = token(client, ["user:reset_password"])
    assert (
        client.post(
            f"/api/v1/users/{user['id']}/password",
            json={"password": PASSWORD},
            headers=bearer(operator["token"]),
        ).status_code
        == 204
    )
    logs = client.get(
        "/api/v1/audit-logs",
        params={"action": "identity.password.reset", "target_id": user["id"]},
    ).json()
    assert logs[0]["details"] == {
        "sessions_revoked": 2,
        "tokens_revoked": 2,
        "credential_id": operator["id"],
    }
    for issued in (personal, second):
        assert client.get("/api/v1/auth/me", headers=bearer(issued["token"])).status_code == 401
    client.cookies.set("keystone_session", old_cookie, domain="testserver.local", path="/")
    assert client.get("/api/v1/auth/me").status_code == 401
    login(client, "alice")
    assert client.get("/api/v1/auth/me").json()["user"]["password_change_required"] is True
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    assert client.get("/api/v1/auth/me", headers=bearer(personal["token"])).status_code == 401


def test_self_password_change_preserves_pat(client):
    login(client)
    personal = token(client, ["user:read"])
    assert (
        client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/users", headers=bearer(personal["token"])).status_code == 200


def test_reset_audit_failure_rolls_back_password_and_all_credential_revocations(
    client, monkeypatch
):
    login(client)
    user = create_user(client)
    login(client, "alice")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "alice", NEW_PASSWORD)
    personal = token(client, [])
    old_cookie = client.cookies.get("keystone_session")
    login(client)
    original = audit.record

    async def fail_reset(*args, **values):
        if args[1] == "identity.password.reset" and values.get("outcome", "success") == "success":
            raise RuntimeError("reset audit failed")
        return await original(*args, **values)

    monkeypatch.setattr(audit, "record", fail_reset)
    assert (
        client.post(f"/api/v1/users/{user['id']}/password", json={"password": PASSWORD}).status_code
        == 500
    )
    client.cookies.set("keystone_session", old_cookie, domain="testserver.local", path="/")
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get("/api/v1/auth/me", headers=bearer(personal["token"])).status_code == 200
    login(client, "alice", NEW_PASSWORD)


def test_global_audit_requires_builtin_admin_and_pat_scope(client):
    login(client)
    allowed = token(client, ["audit:read"])
    limited = token(client, ["user:read"])
    assert client.get("/api/v1/audit-logs", headers=bearer(allowed["token"])).status_code == 200
    assert client.get("/api/v1/audit-logs", headers=bearer(limited["token"])).status_code == 403
    user = create_user(client)
    ordinary = role(client, "ordinary-all", list(PLATFORM_CATALOG.permissions))
    assert assign(client, user["id"], ordinary["id"]).status_code == 204
    login(client, "alice")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "alice", NEW_PASSWORD)
    ordinary_pat = token(client, ["audit:read"])
    for headers in ({}, bearer(ordinary_pat["token"])):
        response = client.get("/api/v1/audit-logs", headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "administrator_required"


def test_audit_transaction_rollback_actor_and_deleted_target(client, monkeypatch):
    admin = login(client)
    initial = client.get(
        "/api/v1/audit-logs", params={"action": "identity.admin.initialize"}
    ).json()
    assert initial[0]["actor_type"] == "system" and initial[0]["actor_id"] is None
    role_value = role(client, "temporary", [])
    personal = token(client, ["role:delete"])
    assert (
        client.delete(
            f"/api/v1/roles/{role_value['id']}", headers=bearer(personal["token"])
        ).status_code
        == 204
    )
    logs = client.get("/api/v1/audit-logs", params={"target_id": role_value["id"]}).json()
    deleted = next(row for row in logs if row["action"] == "identity.role.delete")
    assert deleted["actor_id"] == admin["id"] and deleted["details"]["key"] == "temporary"
    assert deleted["details"]["credential_id"] == personal["id"]
    original = audit.record

    async def fail_success(*args, **kwargs):
        if args[1] == "identity.user.create" and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("test-only failure, secret must never escape")
        return await original(*args, **kwargs)

    monkeypatch.setattr(audit, "record", fail_success)
    response = client.post(
        "/api/v1/users",
        json={"username": "rollback", "display_name": "rollback", "password": PASSWORD},
    )
    assert response.status_code == 500
    assert "secret" not in response.text
    assert (
        query(
            client,
            lambda s: s.scalar(
                select(func.count()).select_from(User).where(User.username == "rollback")
            ),
        )
        == 0
    )
    logs = client.get("/api/v1/audit-logs", params={"action": "identity.user.create"}).json()
    assert len(logs) == 1 and logs[0]["outcome"] == "failure"
    assert logs[0]["actor_id"] == admin["id"]
    client.post(
        "/api/v1/auth/login", headers=BROWSER, json={"username": "unknown", "password": PASSWORD}
    )
    logs = client.get(
        "/api/v1/audit-logs", params={"action": "identity.login", "outcome": "failure"}
    ).json()
    assert logs[0]["actor_type"] == "anonymous" and logs[0]["actor_id"] is None
    assert PASSWORD not in str(logs)
    assert (
        client.patch(f"/api/v1/audit-logs/{deleted['id']}", json={"outcome": "failure"}).status_code
        == 404
    )


def test_scope_union_unknown_permissions_expiry_and_immutable_fields(client):
    admin = login(client)
    controlled = role(client, "control", ["role:update", "role:create"])
    high = role(client, "high", ["audit:read"])
    narrow = token(client, ["role:update", "role:create"])
    # Removing a permission is still management of its previous scope.
    assert (
        client.put(
            f"/api/v1/roles/{high['id']}",
            headers=bearer(narrow["token"]),
            json={"name": "high", "permissions": []},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/roles", json={"key": "bad", "name": "bad", "permissions": ["unknown:read"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/roles", json={"key": "admin", "name": "bad", "permissions": []}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/users/{admin['id']}", json={"username": "renamed", "display_name": "renamed"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/v1/roles/{controlled['id']}",
            json={"key": "renamed", "name": "x", "permissions": []},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/auth/tokens",
            json={"name": "old", "permissions": [], "expires_at": utcnow().isoformat()},
        ).status_code
        == 422
    )
    query(
        client,
        lambda s: s.execute(
            update(PersonalAccessToken).values(expires_at=utcnow() - timedelta(seconds=1))
        ),
    )
    assert client.get("/api/v1/auth/me", headers=bearer(narrow["token"])).status_code == 401
    query(
        client,
        lambda s: s.execute(
            update(LoginSession).values(expires_at=utcnow() - timedelta(seconds=1))
        ),
    )
    assert client.get("/api/v1/auth/me").status_code == 401


def test_concurrent_last_admin_protection(client):
    admin = login(client)
    second = create_user(client, "second")
    admin_role = next(r for r in client.get("/api/v1/roles").json() if r["key"] == "admin")
    assign(client, second["id"], admin_role["id"])
    first_pat = token(client, list(PLATFORM_CATALOG.permissions))
    login(client, "second")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "second", NEW_PASSWORD)
    second_pat = token(client, list(PLATFORM_CATALOG.permissions))

    def disable(identifier, secret):
        return client.put(
            f"/api/v1/users/{identifier}/active", json={"is_active": False}, headers=bearer(secret)
        ).status_code

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(disable, second["id"], first_pat["token"])
        b = pool.submit(disable, admin["id"], second_pat["token"])
        assert sorted([a.result(), b.result()]) == [200, 401]
    assert (
        query(
            client,
            lambda s: s.scalar(
                select(func.count()).select_from(User).where(User.is_active.is_(True))
            ),
        )
        == 1
    )


def test_login_limit_and_production_cookie(client):
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "unknown", "password": PASSWORD},
            headers=BROWSER,
        )
        assert response.status_code == 401
    response = client.post(
        "/api/v1/auth/login", json={"username": "unknown", "password": PASSWORD}, headers=BROWSER
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    # Invalid inputs do not become confirmed actors; the limiter stores no raw username.
    assert "unknown" not in repr(client.app.state.login_limiter.buckets)
    runtime = client.app.state.runtime
    runtime.settings.env = "production"
    runtime.settings.browser_origins = ["https://keystone.test"]
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": "https://keystone.test", "X-Keystone-Request": "1"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "__host-keystone_session=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_unchanged_role_grants_keep_original_creation_audit(client):
    from uuid import UUID

    from keystone.platform.identity.models import RolePermission

    login(client)
    created = role(client, "stable-grant", ["user:read"])

    async def original_grant(session):
        grant = await session.get(RolePermission, (UUID(created["id"]), "user:read"))
        return grant.created_at, grant.created_by

    before = query(client, original_grant)
    response = client.put(
        f"/api/v1/roles/{created['id']}",
        json={"name": "renamed display", "permissions": ["user:read", "audit:read"]},
    )
    assert response.status_code == 200
    assert query(client, original_grant) == before


def test_login_cannot_finish_after_user_disabled(client, monkeypatch):
    login(client)
    user = create_user(client)
    entered, release = Event(), Event()
    passwords = client.app.state.passwords
    original = passwords.verify

    async def paused_verify(encoded, password):
        result = await original(encoded, password)
        entered.set()
        import anyio

        await anyio.to_thread.run_sync(lambda: release.wait(10))
        return result

    monkeypatch.setattr(passwords, "verify", paused_verify)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(
            client.post,
            "/api/v1/auth/login",
            headers=BROWSER,
            json={"username": "alice", "password": PASSWORD},
        )
        try:
            assert entered.wait(5)
            assert (
                client.put(
                    f"/api/v1/users/{user['id']}/active", json={"is_active": False}
                ).status_code
                == 200
            )
        finally:
            release.set()
        assert pending.result().status_code == 401
    from uuid import UUID

    assert (
        query(
            client,
            lambda s: s.scalar(
                select(func.count())
                .select_from(LoginSession)
                .where(LoginSession.user_id == UUID(user["id"]), LoginSession.revoked_at.is_(None))
            ),
        )
        == 0
    )


def test_token_issue_racing_disable_and_initialization_is_one_off(client):
    from uuid import UUID

    from keystone.core.errors import ServiceError
    from keystone.platform.identity.schemas import TokenCreate
    from keystone.platform.identity.schemas.credentials import Credential
    from keystone.platform.identity.services.access import AccessContext
    from keystone.platform.identity.services.tokens import TokenService
    from keystone.platform.identity.services.users import UserService

    login(client)
    user = create_user(client)
    admin_cookie = client.cookies.get("keystone_session")
    login(client, "alice")
    client.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(client, "alice", NEW_PASSWORD)
    user_cookie = client.cookies.get("keystone_session")

    async def race():
        runtime = client.app.state.runtime
        common = (runtime.database, runtime.settings)
        owner = TokenService(AccessContext(*common, Credential("session", user_cookie)))
        admin = UserService(
            AccessContext(*common, Credential("session", admin_cookie)), client.app.state.passwords
        )
        results = await asyncio.gather(
            owner.issue_token(
                TokenCreate(
                    name="racing", permissions=set(), expires_at=utcnow() + timedelta(days=1)
                )
            ),
            admin.set_active(UUID(user["id"]), False),
            return_exceptions=True,
        )
        assert not isinstance(results[1], Exception)
        assert isinstance(results[0], dict) or isinstance(results[0], ServiceError)
        with pytest.raises(ServiceError, match="已有用户"):
            await initialize_admin(
                runtime.database,
                client.app.state.passwords,
                UserCreate(username="another", display_name="another", password=PASSWORD),
            )

    client.portal.call(race)
    assert (
        query(
            client,
            lambda s: s.scalar(
                select(func.count())
                .select_from(PersonalAccessToken)
                .where(
                    PersonalAccessToken.user_id == UUID(user["id"]),
                    PersonalAccessToken.revoked_at.is_(None),
                )
            ),
        )
        == 0
    )
