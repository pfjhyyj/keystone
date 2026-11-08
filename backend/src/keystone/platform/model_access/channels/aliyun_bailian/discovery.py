"""DashScope native model listing; a workspace switches to its dedicated Beijing host."""

import asyncio

from pydantic import TypeAdapter

from keystone.platform.model_access.channels.shared.discovery import model_identifier
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.types import WorkspaceId


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
