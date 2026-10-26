from types import SimpleNamespace

import pytest

from keystone.platform.model_access.schemas.embeddings import EmbeddingRequest, validate_vectors


@pytest.mark.parametrize(
    "texts,dimensions",
    [
        ([""], 2),
        (["x" * 8193], 2),
        (["x"] * 17, 2),
        (["x" * 8192] * 5, 2),
        (["valid"], True),
        (["valid"], 4097),
    ],
)
def test_embedding_request_rejects_unbounded_input(texts, dimensions):
    with pytest.raises(ValueError):
        EmbeddingRequest(texts=texts, dimensions=dimensions)


@pytest.mark.parametrize(
    "vectors",
    [
        [(0, [0.0, 0.0])],
        [(0, [float("inf"), 1])],
        [(0, [True, 1])],
        [(0, ["1", 1])],
        [(0, [1.0])],
        [(1, [1, 0])],
        [(True, [1, 0])],
        [(0, [1, 0]), (0, [1, 0])],
        [(0, [3.5e38, 0])],
    ],
)
def test_embedding_response_requires_exact_indices_finite_nonzero_dimensions(vectors):
    values = [{"index": i, "embedding": v} for i, v in vectors]
    with pytest.raises(ValueError):
        validate_vectors(values, 1, 2)


def test_embedding_response_is_reordered_by_input_index():
    values = [
        {"index": 1, "embedding": [0, 1]},
        {"index": 0, "embedding": [1, 0]},
    ]
    assert validate_vectors(values, 2, 2) == [[1, 0], [0, 1]]


@pytest.mark.parametrize(
    "index,vector",
    [
        (True, [1.0, 2.0]),
        ("0", [1.0, 2.0]),
        (0, [True, 1.0]),
        (0, ["1", 1.0]),
        (0, [0.0, 0.0]),
        (1, [1.0, 2.0]),
        (0, [1.0]),
        (0, [1.0, 2.0]),
    ],
)
async def test_embedding_actual_sdk_response_validation(index, vector):
    import httpx2
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    from keystone.platform.model_access.adapters.inference.embeddings import Embeddings
    from keystone.platform.model_access.schemas.errors import AdapterError

    def respond(request):
        assert request.url.path == "/v1/embeddings"
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "mock-model",
                "data": [{"object": "embedding", "index": index, "embedding": vector}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    async with AsyncOpenAI(
        api_key="mock-only",
        base_url="http://mock.invalid/v1",
        max_retries=0,
        http_client=DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(respond), trust_env=False
        ),
    ) as sdk:
        selected = SimpleNamespace(provider_model="mock-model")
        data = EmbeddingRequest(texts=["test"], dimensions=2)
        if index == 0 and type(index) is int and vector == [1.0, 2.0]:
            assert await Embeddings().invoke(sdk, selected, data) == [vector]
        else:
            with pytest.raises(AdapterError):
                await Embeddings().invoke(sdk, selected, data)
