"""Assembly rejects HTTP routes that would silently shadow another module."""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from keystone.bootstrap import create_app
from keystone.core.config import Settings


@pytest.mark.parametrize("second_path", ["/{identifier}", "/{other_id}", "/{other_id:str}"])
def test_duplicate_nested_routes_fail_at_startup(second_path):
    app = create_app(Settings(config_file=None))
    for path in ("/{identifier}", second_path):
        child = APIRouter()
        child.add_api_route(path, lambda: {}, methods=["GET"], include_in_schema=False)
        parent = APIRouter(prefix="/notes")
        parent.include_router(child)
        app.include_router(parent, prefix="/api/v1/sales")
    with pytest.raises(ValueError, match="Duplicate HTTP route: GET"):
        with TestClient(app):
            pytest.fail("Duplicate routes must prevent startup")


def test_distinct_namespaces_and_methods_remain_available():
    app = create_app(Settings(config_file=None))
    for namespace in ("sales", "support"):
        router = APIRouter(prefix=f"/api/v1/{namespace}/notes")
        router.add_api_route("", lambda: {"ok": True}, methods=["GET", "POST"])
        app.include_router(router)
    with TestClient(app) as client:
        for namespace in ("sales", "support"):
            assert client.get(f"/api/v1/{namespace}/notes").json() == {"ok": True}
            assert client.post(f"/api/v1/{namespace}/notes").status_code == 200
