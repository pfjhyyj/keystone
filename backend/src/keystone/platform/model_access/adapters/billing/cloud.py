"""Bounded cloud accounting requests and explicit signature primitives."""

import base64
import hashlib
import hmac
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


def aliyun_signature(params, secret):
    canonical = query_string(params)
    message = "GET&%2F&" + quote(canonical, safe="-_.~")
    return base64.b64encode(hmac.digest((secret + "&").encode(), message.encode(), "sha1")).decode()


def volcengine_headers(
    keys, host, params, *, timestamp, body=b"", method="GET", region="cn-beijing"
):
    headers = {"host": host, "x-date": timestamp}
    if keys.security_token is not None:
        headers["x-security-token"] = keys.security_token.get_secret_value()
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(name + ":" + headers[name] + "\n" for name in sorted(headers))
    canonical = "\n".join(
        (
            method,
            "/",
            query_string(params),
            canonical_headers,
            signed,
            hashlib.sha256(body).hexdigest(),
        )
    )
    scope = timestamp[:8] + "/" + region + "/billing/request"
    message = "\n".join(
        ("HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    signing_key = keys.access_key_secret.get_secret_value().encode()
    for part in (timestamp[:8], region, "billing", "request"):
        signing_key = hmac.digest(signing_key, part.encode(), "sha256")
    signature = hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()
    identifier = keys.access_key_id.get_secret_value()
    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={identifier}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    if method == "POST":
        headers["content-type"] = "application/json"
    return headers


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
    from keystone.platform.model_access.adapters.billing.contracts import amount

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
