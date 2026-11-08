"""Bounded cloud accounting requests and shared query encoding."""

from urllib.parse import quote

from keystone.platform.model_access.schemas.cloud_billing import CloudBillingCredentials
from keystone.platform.model_access.schemas.errors import AdapterError, ResponseTooLarge


def credentials(secret):
    try:
        return CloudBillingCredentials.model_validate_json(secret)
    except ValueError:
        raise AdapterError(
            409, "model_billing_credentials_required", "请配置云账务 AccessKey ID 和 Secret"
        ) from None


def query_string(params):
    return "&".join(
        quote(str(k), safe="-_.~") + "=" + quote(str(v), safe="-_.~")
        for k, v in sorted(params.items())
    )


async def accounting_request(http, provider, base, *, params, headers=None, body=None):
    def check(response):
        status = response.status_code
        if status in {401, 403}:
            raise AdapterError(
                502, "model_billing_access_denied", "云账务凭据无效、已过期或缺少查询权限"
            )
        if status == 429:
            raise AdapterError(502, "model_rate_limited", "云账务查询过于频繁，请稍后再试")
        if status != 200:
            raise AdapterError(
                502, "model_billing_query_failed", "云账务查询失败，请核对凭据、授权和账期"
            )

    try:
        if body is None:
            result = await http.get_json(
                provider,
                base,
                "/",
                params=params,
                headers=headers,
                check_status=check,
                limit=4 * 1024 * 1024,
            )
        else:
            result = await http.request_json(
                provider,
                base,
                "/",
                method="POST",
                body=body,
                params=params,
                headers=headers,
                check_status=check,
                limit=4 * 1024 * 1024,
            )
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except AdapterError:
        raise
    except ResponseTooLarge:
        raise AdapterError(502, "model_billing_invalid_response", "云账务响应超过上限") from None
    except Exception:
        raise AdapterError(502, "model_billing_query_failed", "云账务查询暂时不可用") from None


def bill_text(value):
    if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid billing label")
    return value


def bill_amounts(item, fields):
    from .contracts import amount

    result = {}
    for name, label in fields:
        if name in item:
            value = amount(item[name], signed=True)
            if value is None:
                raise ValueError("Invalid billed amount")
            result[label] = format(value, "f")
    if not result:
        raise ValueError("Missing billed amounts")
    return result
