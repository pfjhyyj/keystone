"""Read-only model discovery; upstream metadata never grants operation capabilities."""

from keystone.platform.model_access.schemas.errors import AdapterError


async def discover(http, adapter, provider, base, secret):
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
            identifier = item["id"]
            if (
                not isinstance(identifier, str)
                or not 1 <= len(identifier) <= 128
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in identifier)
            ):
                raise ValueError()
            identifiers.add(identifier)
        return [{"id": value} for value in sorted(identifiers)]
    except AdapterError:
        raise
    except (ValueError, KeyError, TypeError, RecursionError):
        raise AdapterError(
            502, "model_invalid_response", "渠道模型列表格式无效或超过上限"
        ) from None
    except Exception:
        raise AdapterError(502, "model_query_failed", "渠道模型列表暂时无法查询") from None
