"""Explicit test manifests; production composition never imports this module."""

from uuid import UUID, uuid4, uuid5

from keystone.platform.flow.schemas.packages import FlowPackage


def package_for_definitions(definitions):
    return FlowPackage(
        key="test-flows",
        host_api_version=1,
        definitions=tuple(definitions),
        execution_modules=("keystone.examples.document_review.workflow",),
    )


def snapshot_for(definition):
    return definition.snapshot(
        package_key="test-flows",
        model_bindings={
            role.key: {
                "model_key": "chat",
                "selection": {
                    "model_entry_id": str(uuid4()),
                    "channel_id": str(uuid4()),
                    "provider_model": "test-model",
                    "provider": "yunwu",
                    "operation": role.operation,
                    "delivery": role.delivery,
                    "api_base": "https://yunwu.ai/v1",
                },
            }
            for role in definition.model_roles
        },
    )


def install_package(client, monkeypatch, package, *, engine=True):
    from keystone import composition

    monkeypatch.setattr(composition, "FLOW_PACKAGES", (package,))
    composition.execution_version.cache_clear()
    runtime = client.app.state.runtime
    runtime.flow_definitions = {definition.key: definition for definition in package.definitions}
    runtime.flow_packages = composition.flow_package_owners()
    if engine:
        from dbos import DBOS

        from keystone.platform.flow.adapters.dbos import Engine

        DBOS.migrate(runtime.settings.database_url.get_secret_value())
        runtime.flow_engine = Engine(runtime.settings)
    return runtime


def assert_correlated(client, run_id, calls, *, require_step=True):
    response = client.get(f"/api/v1/flow-runs/{run_id}/diagnostics")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["models"]["available"] and not value["models"]["truncated"]
    items = value["models"]["items"]
    assert {item["invocation_id"] for item in items} == {str(row.id) for row in calls}
    steps = client.get(f"/api/v1/flow-runs/{run_id}/steps")
    assert steps.status_code == 200, steps.text
    positions = {(row["workflow_id"], row["step_id"]) for row in steps.json()["items"]}
    for item in items:
        assert item["workflow_id"] is not None and type(item["step_id"]) is int
        # A cancelled/in-flight Step need not have an engine history row yet.
        # Its actual send position and deterministic invocation identity remain facts.
        assert (
            str(uuid5(UUID(str(run_id)), f"model:{item['workflow_id']}:{item['step_id']}"))
            == item["invocation_id"]
        )
        if require_step:
            assert (item["workflow_id"], item["step_id"]) in positions
        original = next(row for row in calls if str(row.id) == item["invocation_id"])
        for name in (
            "node_key",
            "model_role",
            "agent_key",
            "agent_call_id",
            "workflow_id",
            "step_id",
        ):
            assert item[name] == original.flow_context.get(name)
    for file in value["files"]["items"]:
        if file["slot"].startswith("artifact.model."):
            assert file["invocation_id"] in {item["invocation_id"] for item in items}
        else:
            assert file["invocation_id"] is None
    assert not any(
        key in response.text
        for key in ("result_context", "cost_context", "checkpoint", "receipt_ids")
    )
    return value
