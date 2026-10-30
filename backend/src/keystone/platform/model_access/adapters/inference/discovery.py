"""Read-only model discovery; upstream metadata never grants operation capabilities."""

import asyncio

from pydantic import TypeAdapter

from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.types import WorkspaceId


def model_identifier(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Invalid model ID")
    return value


async def dashscope_models(http, provider, secret, workspace_id, check):
    host = "https://dashscope.aliyuncs.com"
    if workspace_id is not None:
        workspace = TypeAdapter(WorkspaceId).validate_python(workspace_id)
        host = f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
    identifiers = set()
    total = None
    async with asyncio.timeout(20):
        for page in range(1, 101):
            payload = await http.get_json(
                provider,
                host,
                "/api/v1/models",
                headers={"Authorization": "Bearer " + secret},
                params={
                    "page_no": page,
                    "page_size": 100,
                    "supports": "inference",
                    "inference_providers": "aliyun-bailian",
                    "service_site": "asia-pacific-china",
                },
                limit=262144,
                check_status=check,
            )
            if not isinstance(payload, dict):
                raise ValueError()
            if payload.get("success") is not True or payload.get("code") not in {None, ""}:
                raise AdapterError(
                    502, "model_query_failed", "阿里云模型列表查询失败，请核对 Key 权限"
                )
            output = payload["output"]
            count, size, current, items = (
                output["total"],
                output["page_size"],
                output["page_no"],
                output["models"],
            )
            if (
                type(count) is not int
                or not 0 <= count <= 10000
                or type(size) is not int
                or size != 100
                or type(current) is not int
                or current != page
                or not isinstance(items, list)
                or len(items) != min(100, max(0, count - (page - 1) * 100))
                or total is not None
                and count != total
            ):
                raise ValueError()
            total = count
            for item in items:
                identifiers.add(model_identifier(item["model"]))
            if page * 100 >= total:
                return [{"id": value} for value in sorted(identifiers)]
    raise ValueError("Model list exceeds bounded pagination")


async def discover(http, adapter, provider, base, secret, *, workspace_id=None):
    if not adapter.models_path:
        raise AdapterError(409, "model_discovery_unavailable", "当前渠道未接入模型列表查询")
    if base != adapter.api_base:
        raise AdapterError(409, "model_target_not_allowed", "模型列表查询地址不受支持")

    def check(response):
        if response.status_code == 401:
            raise AdapterError(
                502, "model_authentication_failed", "渠道 Key 无效，无法读取模型列表"
            )
        if response.status_code == 403:
            raise AdapterError(502, "model_access_denied", "渠道 Key 无权读取模型列表")
        if response.status_code == 429:
            raise AdapterError(502, "model_rate_limited", "渠道查询过于频繁，请稍后刷新")
        if response.status_code != 200:
            raise AdapterError(502, "model_query_failed", "渠道模型列表查询失败")

    try:
        if provider == "aliyun_bailian":
            return await dashscope_models(http, provider, secret, workspace_id, check)
        payload = await http.get_json(
            provider,
            base,
            adapter.models_path,
            headers={"Authorization": "Bearer " + secret},
            limit=4 * 1024 * 1024,
            check_status=check,
        )
        data = payload["data"]
        if not isinstance(data, list) or len(data) > 10000:
            raise ValueError()
        identifiers = set()
        for item in data:
            identifiers.add(model_identifier(item["id"]))
        return [{"id": value} for value in sorted(identifiers)]
    except AdapterError:
        raise
    except (ValueError, KeyError, TypeError, RecursionError):
        raise AdapterError(
            502, "model_invalid_response", "渠道模型列表格式无效或超过上限"
        ) from None
    except Exception:
        raise AdapterError(502, "model_query_failed", "渠道模型列表暂时无法查询") from None
