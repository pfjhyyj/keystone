"""Cross-process relay fixtures; always mocked HTTP, never public model traffic."""

import base64
import json
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx2

from support.model_stub import image_bytes
from support.model_stub import transport as original_transport


def install(
    log,
    *,
    kind="url",
    retry=False,
    header=True,
    currency=None,
    model=None,
    unsafe=False,
    generation_error=False,
    crash=None,
    download_format="PNG",
):
    from keystone.platform.model_access.adapters.http import HttpClient

    log = Path(log)

    def record(value):
        with log.open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    async def address(self, host):
        if host == "127.0.0.1":
            from keystone.core.errors import ServiceError

            raise ServiceError(502, "model_result_source_denied", "图片来源不是公网地址")
        return "1.1.1.1"

    def transport(self, provider):
        async def respond(request):
            if request.method == "POST" and request.url.path.endswith(
                ("/images/generations", "/images/edits")
            ):
                if request.url.path.endswith("/edits"):
                    message = BytesParser(policy=default).parsebytes(
                        b"Content-Type: "
                        + request.headers["content-type"].encode()
                        + b"\r\n\r\n"
                        + request.content
                    )
                    fields, count = {}, 0
                    for part in message.iter_parts():
                        key = part.get_param("name", header="content-disposition")
                        if key == "image":
                            count += 1
                        else:
                            fields[key] = part.get_payload(decode=True).decode()
                else:
                    fields, count = json.loads(request.content), 0
                identifier = "bill-" + request.headers["x-keystone-invocation-id"]
                record(
                    {
                        "method": "POST",
                        "path": request.url.path,
                        "receipt": identifier,
                        "model": fields["model"],
                        "references": count,
                        "provider": provider,
                    }
                )
                if crash == "registered":
                    import os

                    os._exit(71)
                n = int(fields["n"])
                if generation_error:
                    return httpx2.Response(502, headers={"x-api-request-id": identifier})
                hostname = "127.0.0.1" if unsafe else "cdn.example.test"
                items = (
                    [{"b64_json": base64.b64encode(image_bytes()).decode()} for _ in range(n)]
                    if kind == "base64"
                    else [
                        {"url": f"https://{hostname}/{i}.png?signature=private-result-secret"}
                        for i in range(n)
                    ]
                )
                return httpx2.Response(
                    200,
                    headers={"x-api-request-id": identifier} if header else {},
                    json={
                        "id": "not-a-billing-id",
                        "created": 1,
                        "data": items,
                        "usage": {"total_tokens": 17, "cost": "999"},
                    },
                )
            if request.method == "GET" and request.headers.get("host") == "cdn.example.test":
                before = sum(c["method"] == "GET" and c["path"].endswith(".png") for c in calls())
                record(
                    {
                        "method": "GET",
                        "path": request.url.path,
                        "auth": "authorization" in request.headers,
                        "sni": request.extensions.get("sni_hostname"),
                    }
                )
                if retry and before == 0:
                    return httpx2.Response(503)
                return httpx2.Response(200, content=image_bytes(download_format))
            if request.url.path == "/api/status":
                policy = {
                    "quota_per_unit": 500000,
                    "currency_type": currency or ("USD" if provider == "openlux" else "CUSTOM"),
                    "custom_currency_exchange_rate": 1,
                }
                return httpx2.Response(200, json={"success": True, "data": policy})
            if request.url.path == "/api/log/token":
                posts = [c for c in calls() if c["method"] == "POST"]
                records = [
                    {
                        "type": 2,
                        "quota": 1328,
                        "model_name": model or p["model"],
                        "other": json.dumps({"request_id": p["receipt"]}),
                    }
                    for p in posts
                ]
                page, size = int(request.url.params["p"]), int(request.url.params["page_size"])
                return httpx2.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": records[(page - 1) * size : page * size],
                            "total": len(records),
                            "page": page,
                            "page_size": size,
                        },
                    },
                )
            return await original_transport(provider).handle_async_request(request)

        return httpx2.MockTransport(respond)

    HttpClient.transport, HttpClient.public_address = transport, address
