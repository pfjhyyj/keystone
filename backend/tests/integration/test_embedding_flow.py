"""Native DBOS/Worker guarantees for text vectors, independent of business indexing."""

import time
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from support.embedding_flow import PACKAGE, definition
from support.flow_packages import assert_correlated, install_package
from test_files_dictionary import client as client
from test_flow import wait_run, worker
from test_identity import bearer, query, token
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.flow.models import FlowRunBudget
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


@pytest.fixture
def embedding(client, monkeypatch):
    install_package(client, monkeypatch, PACKAGE)
    client.app.state.runtime.settings.flow_worker_concurrency = 1
    client.app.state.runtime.settings.flow_global_concurrency = 1
    configured = entry(client, channel(client), key="embed", operations=["text.embed"])
    return client, configured


def start(client, *, headers=None):
    body = {
        "definition_key": definition.key,
        "request_key": str(uuid4()),
        "input": {"texts": ["scene", "action"], "dimensions": 3},
        "model_bindings": {"embed": "embed"},
    }
    response = client.post("/api/v1/_test/review", json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert (
        client.post("/api/v1/_test/review", json=body, headers=headers).json()["id"]
        == response.json()["id"]
    )
    return response.json()


def bootstrap(journal, mode="normal"):
    return (
        "from support.embedding_flow import install; "
        f"install({str(journal)!r}, mode={mode!r}); "
        "from keystone.entrypoints.worker import main; main()"
    )


def calls(client):
    return query(client, lambda session: session.scalars(select(ModelInvocation))).all()


def test_embedding_uses_original_role_budget_cost_and_checkpoint(embedding, tmp_path):
    client, configured = embedding
    run, journal = start(client), tmp_path / "calls.txt"
    with worker(client, tmp_path, code=bootstrap(journal)) as (process, log):
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == "succeeded", done
        assert done["result"]["vectors"] == [[1.0] * 3, [1.0] * 3]
        records = calls(client)
        assert_correlated(client, run["id"], records)
    assert journal.read_text().splitlines() == [done["result"]["invocation_id"]]
    assert len(records) == 1 and records[0].model_entry_id == UUID(configured["id"])
    assert records[0].operation == "text.embed" and records[0].result_context is None
    assert records[0].status == "succeeded" and records[0].cost_amount == Decimal("0.0002")
    assert records[0].usage == {"input_tokens": 9, "total_tokens": 9}
    budget = query(client, lambda session: session.get(FlowRunBudget, UUID(run["id"])))
    assert budget.counters["model_requests"] == 1 and budget.counters["input_tokens"] == 9
    assert budget.counters.get("output_tokens", 0) == 0


@pytest.mark.parametrize("change", ["cancel", "revoke", "capability"])
def test_embedding_rechecks_authority_and_cancel_before_send(embedding, tmp_path, change):
    client, configured = embedding
    pat = token(client, list(client.get("/api/v1/auth/me").json()["permissions"]))
    run, journal = start(client, headers=bearer(pat["token"])), tmp_path / "calls.txt"
    if change == "cancel":
        assert client.post(f"/api/v1/flow-runs/{run['id']}/cancel").status_code == 200
    elif change == "revoke":
        assert client.delete("/api/v1/auth/tokens/" + pat["id"]).status_code == 204
    else:
        changed = client.patch(
            "/api/v1/model-entries/" + configured["id"],
            json={
                "channel_id": configured["channel_id"],
                "provider_model": configured["provider_model"],
                "operations": ["text.generate"],
            },
        )
        assert changed.status_code == 200, changed.text
    with worker(client, tmp_path, code=bootstrap(journal)) as (process, log):
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == ("cancelled" if change == "cancel" else "failed"), done
    assert not journal.exists() and not calls(client)


@pytest.mark.parametrize("change", ["cancel", "revoke"])
def test_embedding_cancel_or_revoke_during_send_does_not_repeat_or_publish(
    embedding, tmp_path, change
):
    client, _ = embedding
    pat = token(client, list(client.get("/api/v1/auth/me").json()["permissions"]))
    run, journal = start(client, headers=bearer(pat["token"])), tmp_path / "calls.txt"
    with worker(client, tmp_path, code=bootstrap(journal, "gate")) as (process, log):
        deadline = time.monotonic() + 30
        while not journal.exists() and time.monotonic() < deadline:
            assert process.poll() is None, log.read_text(encoding="utf8")
            time.sleep(0.1)
        assert journal.exists(), log.read_text(encoding="utf8")
        if change == "cancel":
            assert client.post(f"/api/v1/flow-runs/{run['id']}/cancel").status_code == 200
        else:
            assert client.delete("/api/v1/auth/tokens/" + pat["id"]).status_code == 204
        journal.with_suffix(".release").touch()
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == ("cancelled" if change == "cancel" else "failed"), done
        assert done["result"] is None
    records = calls(client)
    assert len(records) == 1 and records[0].status in {"unknown", "succeeded"}
    assert journal.read_text().splitlines() == [str(records[0].id)]


@pytest.mark.parametrize("point", ["sent", "completed"])
def test_embedding_crash_after_send_never_resends_without_saved_result(embedding, tmp_path, point):
    client, _ = embedding
    run, journal = start(client), tmp_path / "calls.txt"
    with worker(client, tmp_path, code=bootstrap(journal, point)) as (process, log):
        assert process.wait(timeout=40) == 73, log.read_text(encoding="utf8")
    before = calls(client)
    assert len(before) == 1
    with worker(client, tmp_path, code=bootstrap(journal)) as (process, log):
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == "failed" and done["error_code"] == "model_result_unavailable", done
    after = calls(client)
    assert len(after) == 1 and after[0].id == before[0].id
    assert after[0].status == ("unknown" if point == "sent" else "succeeded")
    assert journal.read_text().splitlines() == [str(after[0].id)]
