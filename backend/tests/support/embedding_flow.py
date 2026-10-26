"""Independent embedding contract and deterministic fault injection; no business imports."""

from keystone.platform.flow.schemas.budgets import FlowLimits
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage
from keystone.platform.model_access.schemas.embeddings import EmbeddingRequest, EmbeddingResult

definition = FlowDefinition(
    key="test.embedding",
    name="Embedding fixture",
    description="Test only",
    revision="1",
    input_model=EmbeddingRequest,
    output_model=EmbeddingResult,
    entrypoint="test.embedding.run",
    required_permissions=("model:invoke",),
    model_roles=(ModelRole("embed", "Text vectors", "text.embed"),),
    limits=FlowLimits(model_requests=1, output_tokens=0),
)

PACKAGE = FlowPackage(
    key="embedding-test",
    host_api_version=1,
    definitions=(definition,),
    execution_modules=("support.embedding_flow",),
    execution_packages=("support",),
)
MODE = "normal"


def register():
    from dbos import DBOS

    from keystone.platform.flow.workflows.context import flow_context

    @DBOS.step(name="test.embedding.invoke", retries_allowed=False)
    async def embed(run_id):
        ctx = flow_context(run_id)
        result = await ctx.invoke_embedding("embed", await ctx.inputs(), node_key="embed")
        if MODE == "completed":
            import os

            os._exit(73)
        return result.model_dump(mode="json")

    @DBOS.workflow(name=definition.entrypoint)
    async def run(run_id):
        return await embed(run_id)


def install(journal, *, mode="normal"):
    import asyncio
    import json
    import os
    from pathlib import Path

    import httpx2

    from keystone import composition
    from keystone.platform.model_access.adapters.http import HttpClient
    from support.model_stub import transport as fallback

    global MODE
    MODE = mode
    composition.FLOW_PACKAGES = (PACKAGE,)
    composition.execution_version.cache_clear()
    path = Path(journal)

    def transport(self, provider):
        async def respond(request):
            if not request.url.path.endswith("/embeddings"):
                return await fallback(provider).handle_async_request(request)
            with path.open("a", encoding="utf8") as stream:
                stream.write(request.headers["X-Keystone-Invocation-Id"] + "\n")
            if mode == "sent":
                os._exit(73)
            if mode == "gate":
                while not path.with_suffix(".release").exists():
                    await asyncio.sleep(0.05)
            body = json.loads(request.content)
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "id": "mock-embedding",
                    "model": body["model"],
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [1.0] * body["dimensions"]}
                        for i in range(len(body["input"]))
                    ],
                    "usage": {"prompt_tokens": 9, "total_tokens": 9, "cost": 0.0002},
                },
            )

        return httpx2.MockTransport(respond)

    HttpClient.transport = transport
