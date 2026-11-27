"""Native SQL scope-before-page and safe Run/Model/File correlation."""

import hashlib
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import select
from support.flow_admission import accept
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import (
    NEW_PASSWORD,
    PASSWORD,
    assign,
    bearer,
    create_user,
    login,
    query,
    role,
    token,
)
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.core.time import utcnow
from keystone.platform.files.models import FileReference
from keystone.platform.flow.models import FlowRun
from keystone.platform.identity.models import User
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.images import (
    ImageManifest,
    ImageOutput,
    ImageTaskReceipt,
)

pytestmark = pytest.mark.postgres


def seed(client):
    admin_id = UUID(client.get("/api/v1/auth/me").json()["user"]["id"])
    alice = create_user(client)
    access = role(
        client,
        "diagnostics",
        [
            "flow:read",
            "test_files.asset:inspect",
            "test_files.asset:content_read",
            "model_invocation:read",
        ],
    )
    assign(client, alice["id"], access["id"])
    response, _ = accept(client)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    configured = entry(client, channel(client, name="Diagnostic channel"), name="Diagnostic model")
    finished_at = utcnow()
    ids = [uuid4() for _ in range(4)]
    files, manifests = [], []
    for index in range(4):
        image = BytesIO()
        Image.new("RGB", (2, 2), (index * 60, 0, 100)).save(image, format="PNG")
        content = image.getvalue()
        file_id = upload(
            client, content, key=f"diagnostic-{index}", content_type="image/png"
        ).json()["id"]
        submitted = utcnow() - timedelta(days=90)
        manifests.append(
            ImageManifest(
                request_digest="a" * 64,
                operation="image.generate",
                outputs=[
                    ImageOutput(
                        artifact_key=f"model.{ids[index]}.0",
                        checksum_sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                        content_type="image/png",
                    )
                ],
                task=ImageTaskReceipt(
                    task_id=f"result-secret-{index}",
                    request_digest="a" * 64,
                    count=1,
                    submitted_at=submitted,
                    deadline_at=submitted + timedelta(seconds=300),
                    next_query_at=submitted,
                ),
            ).model_dump(mode="json")
        )
        files.append(file_id)

    async def insert(session):
        run = await session.get(FlowRun, run_id)
        run.actor_id = UUID(alice["id"])
        for index, identifier in enumerate(ids):
            session.add(
                ModelInvocation(
                    id=identifier,
                    user_id=UUID(alice["id"]) if index % 2 else admin_id,
                    model_entry_id=UUID(configured["id"]),
                    channel_id=UUID(configured["channel_id"]),
                    provider_model="mock-chat",
                    status="unknown",
                    finished_at=finished_at,
                    created_at=finished_at - timedelta(days=90, seconds=4 - index),
                    flow_context={
                        "flow_run_id": str(run_id),
                        "node_key": "generate",
                        "model_role": "generate",
                        **(
                            {"workflow_id": "workflow-original", "step_id": 3} if index == 1 else {}
                        ),
                    },
                    operation="image.generate",
                    error_code="private-exception-secret",
                    result_context=manifests[index],
                    cost_context={"private": "cost-secret"},
                    receipt_ids={"private": "receipt-secret"},
                    upstream_request_id="provider-secret",
                )
            )
            session.add(
                FileReference(
                    owner=f"flow:{run_id}",
                    file_id=UUID(files[index]),
                    slot=f"artifact.model.{identifier}.0",
                )
            )

    query(client, insert)
    login(client, "alice")
    assert (
        client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    login(client, "alice", NEW_PASSWORD)
    return run_id, ids, files


def test_permissions_filter_before_pagination_without_default_history_window(client):
    run_id, ids, files = seed(client)
    path = f"/api/v1/flow-runs/{run_id}/diagnostics"
    first = client.get(path, params={"section": "models", "limit": 1})
    assert first.status_code == 200, first.text
    page = first.json()["models"]
    assert "total" not in page and first.json()["files"] is None
    assert page["available"] and page["truncated"] and page["next_offset"] == 1
    assert [item["invocation_id"] for item in page["items"]] == [str(ids[1])]
    assert page["items"][0]["workflow_id"] == "workflow-original"
    assert page["items"][0]["step_id"] == 3
    assert page["items"][0]["error_code"] == "model_response_unknown"
    assert page["items"][0]["model_name"] == "Diagnostic model"
    assert page["items"][0]["channel_name"] == "Diagnostic channel"
    assert page["items"][0]["provider_model"] == "mock-chat"
    assert page["items"][0]["duration_ms"] == (90 * 86400 + 3) * 1000
    second = client.get(path, params={"section": "models", "limit": 1, "offset": 1}).json()[
        "models"
    ]
    assert [item["invocation_id"] for item in second["items"]] == [str(ids[3])]
    assert second["items"][0]["workflow_id"] is None and second["items"][0]["step_id"] is None
    assert not second["truncated"] and second["next_offset"] is None
    # Authorized rows are 90 days old; private evidence is never included, even as text.
    for value in (
        "result-secret",
        "cost-secret",
        "receipt-secret",
        "provider-secret",
        "private-exception-secret",
    ):
        assert value not in first.text

    async def add_other_run_facts(session):
        invocation = await session.get(ModelInvocation, ids[1])
        other_run = uuid4()
        session.add_all(
            (
                FileReference(owner=f"flow:{other_run}", file_id=UUID(files[1]), slot="input"),
                ModelInvocation(
                    user_id=invocation.user_id,
                    model_entry_id=invocation.model_entry_id,
                    channel_id=invocation.channel_id,
                    provider_model=invocation.provider_model,
                    operation=invocation.operation,
                    flow_context={**invocation.flow_context, "flow_run_id": str(other_run)},
                ),
            )
        )

    query(client, add_other_run_facts)
    counted = client.get(path, params={"include_total": True, "limit": 1})
    assert counted.status_code == 200, counted.text
    assert counted.json()["models"] == {**page, "total": 2}
    assert counted.json()["files"]["total"] == 4
    beyond = client.get(path, params={"include_total": True, "limit": 1, "offset": 4})
    assert beyond.status_code == 200, beyond.text
    for section, total in (("models", 2), ("files", 4)):
        value = beyond.json()[section]
        assert value["total"] == total and value["items"] == []
        assert not value["truncated"] and value["next_offset"] is None
    files_only = client.get(path, params={"section": "files", "include_total": True})
    assert files_only.json()["models"] is None and files_only.json()["files"]["total"] == 4

    file_rows = []
    for offset in range(4):
        response = client.get(path, params={"section": "files", "limit": 1, "offset": offset})
        assert response.status_code == 200, response.text
        assert "total" not in response.json()["files"]
        file_rows += response.json()["files"]["items"]
    assert {row["file_id"] for row in file_rows} == set(files)
    assert {row["invocation_id"] for row in file_rows} == {None, str(ids[1]), str(ids[3])}
    # Inspect and retained reference do not confer content access.
    assert client.get(f"/api/v1/_test/materials/{files[1]}/content").status_code == 404
    assert client.get(path, params={"limit": 101}).status_code == 422
    assert client.get(path, params={"offset": -1}).status_code == 422
    assert client.get(path, params={"section": "checkpoints"}).status_code == 422


def test_run_visibility_never_grants_other_resource_permissions(client):
    run_id, ids, _ = seed(client)
    path = f"/api/v1/flow-runs/{run_id}/diagnostics"
    # A PAT can read Run file IDs/slots without gaining model invocation facts.
    limited = bearer(token(client, ["flow:read"])["token"])
    response = client.get(path, headers=limited)
    assert response.status_code == 200, response.text
    assert response.json()["models"] == {
        "available": False,
        "items": [],
        "truncated": False,
        "next_offset": None,
    }
    assert len(response.json()["files"]["items"]) == 4
    assert all(item["invocation_id"] is None for item in response.json()["files"]["items"])
    counted = client.get(path, params={"include_total": True}, headers=limited)
    assert counted.status_code == 200, counted.text
    assert counted.json()["models"] == response.json()["models"]
    assert counted.json()["files"]["total"] == 4

    metadata_only = bearer(token(client, ["flow:read", "test_files.asset:inspect"])["token"])
    metadata = client.get(path, headers=metadata_only)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["models"]["available"] is False
    assert len(metadata.json()["files"]["items"]) == 4
    assert all(item["invocation_id"] is None for item in metadata.json()["files"]["items"])

    # Loss of Run ownership is enforced even while file/model permissions remain.
    async def move(session):
        run = await session.get(FlowRun, run_id)
        admin = await session.scalar(select(User).where(User.username == "admin"))
        run.actor_id = admin.id

    query(client, move)
    assert client.get(path, params={"include_total": True}).status_code == 404
    assert client.get(f"/api/v1/flow-runs/{uuid4()}/diagnostics").status_code == 404


def test_totals_include_zero_for_visible_empty_sections(client):
    accepted, _ = accept(client)
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["id"]
    response = client.get(f"/api/v1/flow-runs/{run_id}/diagnostics?include_total=true")
    assert response.status_code == 200, response.text
    models, files = response.json()["models"], response.json()["files"]
    assert models["available"] and models["total"] == 0 and models["items"] == []
    assert files["total"] == 0 and files["items"] == []


def test_flow_manager_does_not_gain_read_all_model_scope(client):
    run_id, ids, _ = seed(client)
    login(client)
    headers = bearer(token(client, ["flow:read", "flow:manage", "model_invocation:read"])["token"])
    response = client.get(f"/api/v1/flow-runs/{run_id}/diagnostics", headers=headers)
    assert response.status_code == 200, response.text
    assert {item["invocation_id"] for item in response.json()["models"]["items"]} == {
        str(ids[0]),
        str(ids[2]),
    }
    # The administrator's inspect exception does not widen model read_all scope.
    metadata_headers = bearer(
        token(
            client,
            ["flow:read", "flow:manage", "model_invocation:read", "test_files.asset:inspect"],
        )["token"]
    )
    metadata = client.get(f"/api/v1/flow-runs/{run_id}/diagnostics", headers=metadata_headers)
    assert metadata.status_code == 200, metadata.text
    assert len(metadata.json()["files"]["items"]) == 4
    for item in metadata.json()["files"]["items"]:
        source = next(identifier for identifier in ids if str(identifier) in item["slot"])
        assert item["invocation_id"] == (str(source) if source in {ids[0], ids[2]} else None)
    # Even the admin identity's PAT needs the Flow object scope explicitly.
    no_manage = bearer(
        token(client, ["flow:read", "model_invocation:read_all", "model_invocation:read"])["token"]
    )
    assert (
        client.get(f"/api/v1/flow-runs/{run_id}/diagnostics", headers=no_manage).status_code == 404
    )


def test_source_requires_exact_manifest_and_file_metadata_without_exposing_evidence(client):
    run_id, ids, files = seed(client)
    path = f"/api/v1/flow-runs/{run_id}/diagnostics?section=files"
    target = f"artifact.model.{ids[1]}.0"
    original = query(client, lambda session: session.get(ModelInvocation, ids[1])).result_context

    async def alias_index(session):
        session.add(
            FileReference(
                owner=f"flow:{run_id}", slot=f"artifact.model.{ids[1]}.1", file_id=UUID(files[1])
            )
        )

    query(client, alias_index)
    # The full Manifest mutation matrix is in unit/test_flow_diagnostic_contracts.py.
    changed = deepcopy(original)
    changed["outputs"][0]["checksum_sha256"] = "b" * 64
    changes = [changed]

    async def replace_evidence(session, evidence):
        (await session.get(ModelInvocation, ids[1])).result_context = evidence

    for evidence in changes:
        query(client, lambda session, evidence=evidence: replace_evidence(session, evidence))
        response = client.get(path)
        assert response.status_code == 200, response.text
        item = next(row for row in response.json()["files"]["items"] if row["slot"] == target)
        assert item == {"file_id": files[1], "slot": target, "invocation_id": None}
        # A corrupted Manifest cannot move output zero to a forged index-one key,
        # even when a retained File has the exact bytes and that forged slot.
        alias = next(
            row
            for row in response.json()["files"]["items"]
            if row["slot"] == f"artifact.model.{ids[1]}.1"
        )
        assert alias["invocation_id"] is None
        assert all(
            set(row) == {"file_id", "slot", "invocation_id"}
            for row in response.json()["files"]["items"]
        )
        assert "result-secret" not in response.text
        assert (
            query(client, lambda session: session.get(ModelInvocation, ids[1])).result_context
            == evidence
        )
    query(client, lambda session: replace_evidence(session, original))
    item = next(row for row in client.get(path).json()["files"]["items"] if row["slot"] == target)
    assert item["invocation_id"] == str(ids[1])
    original_context = query(
        client, lambda session: session.get(ModelInvocation, ids[1])
    ).flow_context

    async def move_call(session, context):
        (await session.get(ModelInvocation, ids[1])).flow_context = context

    query(
        client,
        lambda session: move_call(session, {**original_context, "flow_run_id": str(uuid4())}),
    )
    response = client.get(path)
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["files"]["items"] if row["slot"] == target)
    assert item["invocation_id"] is None  # Matching bytes do not cross a Run boundary.
    query(client, lambda session: move_call(session, original_context))


def test_same_slot_wrong_file_cannot_borrow_another_files_source_confirmation(client):
    run_id, ids, files = seed(client)
    slot = f"artifact.model.{ids[1]}.0"

    async def alias(session):
        session.add_all(
            [
                # One correct File and one wrong File share a candidate source key.
                FileReference(owner=f"flow:{run_id}", slot=slot, file_id=UUID(files[3])),
                # Same bytes cannot invent a nonexistent output index or a report edge.
                FileReference(
                    owner=f"flow:{run_id}",
                    slot=f"artifact.model.{ids[1]}.1",
                    file_id=UUID(files[1]),
                ),
                FileReference(
                    owner=f"flow:{run_id}",
                    slot=f"artifact.model.{ids[1]}.2",
                    file_id=UUID(files[1]),
                ),
                FileReference(
                    owner=f"flow:{run_id}", slot="artifact.report", file_id=UUID(files[1])
                ),
            ]
        )

    query(client, alias)
    response = client.get(f"/api/v1/flow-runs/{run_id}/diagnostics?section=files")
    assert response.status_code == 200, response.text
    rows = {
        (row["file_id"], row["slot"]): row["invocation_id"]
        for row in response.json()["files"]["items"]
    }
    assert rows[(files[1], slot)] == str(ids[1])
    assert rows[(files[3], slot)] is None
    assert rows[(files[3], f"artifact.model.{ids[3]}.0")] == str(ids[3])
    assert rows[(files[1], f"artifact.model.{ids[1]}.1")] is None
    assert rows[(files[1], f"artifact.model.{ids[1]}.2")] is None
    assert rows[(files[1], "artifact.report")] is None


def test_step_diagnostics_hidden_from_files_output_preview_and_expiration_prune(
    client,
):
    from keystone.platform.files.models import File
    from keystone.platform.flow.services.retention import prune_flow_diagnostics

    run_id, _, files = seed(client)
    login(client)
    diagnostic_file = upload(
        client, b'{"attempt":1,"label":"test"}', key="step-diag", content_type="application/json"
    ).json()["id"]
    webp_bytes = BytesIO()
    Image.new("RGB", (4, 4), (20, 80, 160)).save(webp_bytes, format="WEBP")
    output_webp = upload(
        client, webp_bytes.getvalue(), key="output-webp", content_type="image/webp"
    ).json()["id"]

    async def attach(session):
        from sqlalchemy import delete

        run = await session.get(FlowRun, run_id)
        run.status = "succeeded"
        run.result = {"result_file_id": output_webp}
        run.finished_at = utcnow() - timedelta(days=20)
        # Drop the test-fixture material reference so diagnostic_file is held solely by this Run.
        await session.execute(
            delete(FileReference).where(FileReference.file_id == UUID(diagnostic_file))
        )
        session.add_all(
            [
                FileReference(
                    owner=f"flow:{run_id}",
                    slot="artifact.diagnostic.step.abcd1234.1.0000000001",
                    file_id=UUID(diagnostic_file),
                ),
                FileReference(
                    owner=f"flow:{run_id}",
                    slot="output",
                    file_id=UUID(output_webp),
                ),
            ]
        )

    query(client, attach)
    # Internal step diagnostic slots never leak into business file diagnostics.
    diag = client.get(f"/api/v1/flow-runs/{run_id}/diagnostics", params={"section": "files"})
    assert diag.status_code == 200, diag.text
    slots = [item["slot"] for item in diag.json()["files"]["items"]]
    assert not any(slot.startswith("artifact.diagnostic.step.") for slot in slots)
    assert "output" in slots

    # Output file preview enforces flow:diagnose and output slot membership, and supports WebP.
    preview = client.get(f"/api/v1/flow-runs/{run_id}/output-file", params={"file_id": output_webp})
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/webp"
    assert (
        client.get(
            f"/api/v1/flow-runs/{run_id}/output-file", params={"file_id": files[0]}
        ).status_code
        == 404
    )
    no_diagnose = bearer(token(client, ["flow:read"])["token"])
    assert (
        client.get(
            f"/api/v1/flow-runs/{run_id}/output-file",
            params={"file_id": output_webp},
            headers=no_diagnose,
        ).status_code
        == 403
    )

    # Retention preview counts expired diagnostic files without mutating; apply prunes only them.
    database = client.app.state.runtime.database
    assert client.portal.call(lambda: prune_flow_diagnostics(database, 14, apply=False)) == 1
    assert client.portal.call(lambda: prune_flow_diagnostics(database, 30, apply=True)) == 0
    assert client.portal.call(lambda: prune_flow_diagnostics(database, 14, apply=True)) == 1
    assert client.portal.call(lambda: prune_flow_diagnostics(database, 14, apply=True)) == 0

    diag_file_row = query(client, lambda s: s.get(File, UUID(diagnostic_file)))
    output_file_row = query(client, lambda s: s.get(File, UUID(output_webp)))
    assert diag_file_row.status == "deleting"
    assert output_file_row.status == "active"
