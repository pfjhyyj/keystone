"""Native DashScope catalog projection, bounded pagination and credential targets."""

from copy import deepcopy

import httpx2
import pytest

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.channels import CHANNELS
from keystone.platform.model_access.schemas.channels import ChannelCreate

BASE = CHANNELS["aliyun_bailian"].api_base


def native_page(models, *, total=None, page=1):
    return {
        "success": True,
        "code": None,
        "output": {
            "total": len(models) if total is None else total,
            "page_no": page,
            "page_size": 100,
            "models": [{"model": model, "prices": "never project"} for model in models],
        },
    }


@pytest.mark.parametrize("workspace", [None, "llm-example"])
async def test_native_catalog_uses_saved_inference_key_and_follows_pages_without_metadata(
    workspace,
):
    calls = []
    models = [f"model-{i:03d}" for i in range(105)]
    models[-1] = models[0]

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/api/v1/models"
        assert request.url.host == (
            f"{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else "dashscope.aliyuncs.com"
        )
        assert request.headers["authorization"] == "Bearer inference-secret"
        assert "inference-secret" not in str(request.url)
        page = int(request.url.params["page_no"])
        assert dict(request.url.params) == {
            "page_no": str(page),
            "page_size": "100",
            "supports": "inference",
            "inference_providers": "aliyun-bailian",
            "service_site": "asia-pacific-china",
        }
        return httpx2.Response(
            200, json=native_page(models[(page - 1) * 100 : page * 100], total=105, page=page)
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    result = await InferenceClient(http).list_models(
        "aliyun_bailian", BASE, "inference-secret", workspace_id=workspace
    )
    assert result == [{"id": name} for name in sorted(set(models))]
    assert len(calls) == 2 and "prices" not in str(result)


async def test_empty_native_catalog_returns_empty_list_after_one_request():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json=native_page([]))

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    assert await InferenceClient(http).list_models("aliyun_bailian", BASE, "secret") == []
    assert len(calls) == 1


@pytest.mark.parametrize(
    "workspace", ["evil.invalid", "a/b", "-a", "a-", "a_b", "UPPER", "a" * 64, "a\nb", ""]
)
async def test_workspace_cannot_redirect_saved_credentials(workspace):
    calls = []
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(lambda request: calls.append(request))
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models(
            "aliyun_bailian", BASE, "secret", workspace_id=workspace
        )
    assert error.value.code == "model_invalid_response" and not calls
    with pytest.raises(ValueError):
        ChannelCreate(
            name="ali",
            provider="aliyun_bailian",
            api_base=BASE,
            api_key="secret",
            workspace_id=workspace,
        )


@pytest.mark.parametrize(
    "base",
    [
        "https://evil.invalid",
        BASE + "/other",
        "https://dashscope.aliyuncs.com.evil.invalid/compatible-mode/v1",
    ],
)
async def test_inference_base_must_match_installed_channel_before_model_query(base):
    http = HttpClient()
    http.transport = lambda _: pytest.fail("untrusted target must not send")
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models("aliyun_bailian", base, "secret")
    assert error.value.code == "model_target_not_allowed"


@pytest.mark.parametrize(
    "invalid",
    [
        "total_bool",
        "total_string",
        "negative",
        "too_many",
        "page_bool",
        "wrong_page",
        "wrong_size",
        "missing_row",
        "extra_row",
        "wrong_items",
        "missing_id",
        "invalid_id",
        "wrong_output",
        "error",
        "wrong_root",
    ],
)
async def test_invalid_native_catalog_never_returns_partial_or_unbounded_results(invalid):
    payload = deepcopy(native_page(["qwen-plus"]))
    output = payload["output"]
    if invalid == "total_bool":
        output["total"] = True
    elif invalid == "total_string":
        output["total"] = "1"
    elif invalid == "negative":
        output["total"] = -1
    elif invalid == "too_many":
        output["total"] = 10001
    elif invalid == "page_bool":
        output["page_no"] = True
    elif invalid == "wrong_page":
        output["page_no"] = 2
    elif invalid == "wrong_size":
        output["page_size"] = 20
    elif invalid == "missing_row":
        output["models"] = []
    elif invalid == "extra_row":
        output["models"] *= 2
    elif invalid == "wrong_items":
        output["models"] = {}
    elif invalid == "missing_id":
        output["models"] = [{}]
    elif invalid == "invalid_id":
        output["models"][0]["model"] = "invalid id"
    elif invalid == "wrong_output":
        payload["output"] = None
    elif invalid == "error":
        payload.update(success=False, message="private upstream body")
    else:
        payload = []
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json=payload)

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models("aliyun_bailian", BASE, "secret")
    assert error.value.code in {"model_invalid_response", "model_query_failed"}
    assert "private" not in error.value.detail and len(calls) == 1


@pytest.mark.parametrize("failure", ["total_changed", "empty_page", "http_error"])
async def test_mid_query_changes_and_failures_discard_previously_fetched_ids(failure):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(
                200, json=native_page([f"model-{i}" for i in range(100)], total=101)
            )
        if failure == "http_error":
            return httpx2.Response(500, text="private error")
        return httpx2.Response(
            200,
            json=native_page(
                [] if failure == "empty_page" else ["last"],
                total=102 if failure == "total_changed" else 101,
                page=2,
            ),
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError):
        await InferenceClient(http).list_models("aliyun_bailian", BASE, "secret")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "model_authentication_failed"),
        (403, "model_access_denied"),
        (429, "model_rate_limited"),
        (302, "model_query_failed"),
    ],
)
async def test_native_discovery_sanitizes_errors_and_never_falls_back_to_another_host(status, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            status, text="private body", headers={"Location": "https://evil.invalid"}
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models(
            "aliyun_bailian", BASE, "secret", workspace_id="llm-private"
        )
    assert error.value.code == code and "private" not in error.value.detail
    assert len(calls) == 1 and calls[0].url.host == "llm-private.cn-beijing.maas.aliyuncs.com"


async def test_native_page_response_size_is_bounded():
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(
        lambda _: httpx2.Response(200, content=b"x" * 262145)
    )
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models("aliyun_bailian", BASE, "secret")
    assert error.value.code == "model_invalid_response"
