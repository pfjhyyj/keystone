"""Deterministic upstream simulation, exercised through the real SDK HTTP transport.

This module never constructs a network transport. Mock amounts are test receipts only.
"""

import json
from pathlib import Path

import httpx2

IMAGE_TASK_STATE = None


def task_state(change):
    path = Path(IMAGE_TASK_STATE) if IMAGE_TASK_STATE else None
    state = json.loads(path.read_text()) if path and path.exists() else {}
    result = change(state)
    if path:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(path)
    return result


def image_bytes(format="PNG"):
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format=format)
    return output.getvalue()


class MockStream(httpx2.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def transport(provider):
    async def respond(request):
        if request.method == "POST" and request.url.path.endswith("/kling/v1/images/generations"):
            body = json.loads(request.content)
            task_id = "task-" + request.headers["x-keystone-invocation-id"]
            task_state(lambda state: state.setdefault(task_id, {"n": body["n"], "queries": 0}))
            return httpx2.Response(
                200,
                headers={"x-api-request-id": "native-" + task_id},
                json={
                    "code": 0,
                    "data": {"task_id": task_id, "task_status": "submitted"},
                },
            )
        if request.method == "GET" and "/kling/v1/images/generations/" in request.url.path:
            task_id = request.url.path.rsplit("/", 1)[1]

            def query(state):
                task = state.setdefault(task_id, {"n": 1, "queries": 0})
                task["queries"] += 1
                return dict(task)

            task = task_state(query)
            pending = IMAGE_TASK_STATE is not None and task["queries"] == 1
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": task_id,
                        "task_status": "processing" if pending else "succeed",
                        "task_result": {
                            "images": [
                                {"url": f"https://p2-fdl.klingai.com/{task_id}/{i}.png"}
                                for i in range(task["n"])
                            ]
                        },
                    },
                },
            )
        if request.method == "GET" and request.headers.get("host") == "p2-fdl.klingai.com":
            return httpx2.Response(
                200, content=image_bytes(), headers={"content-type": "image/png"}
            )
        if request.method == "POST" and request.url.path.endswith("/images"):
            import base64
            from io import BytesIO

            from PIL import Image

            body = json.loads(request.content)
            output = BytesIO()
            Image.new("RGB", (2, 2), "blue").save(output, format=body["output_format"])
            encoded = base64.b64encode(output.getvalue()).decode()
            return httpx2.Response(
                200,
                json={
                    "id": "mock-" + request.headers["x-keystone-invocation-id"],
                    "data": [
                        {"b64_json": encoded, "media_type": "image/" + body["output_format"]}
                        for _ in range(body["n"])
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "total_tokens": 20,
                        "cost": "0.0012",
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            identifier = "mock-" + request.headers["x-keystone-invocation-id"]
            usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
            if provider == "openrouter":
                usage["cost"] = "0.0012"
            text = "模拟渠道响应：Keystone 模型调用链路已连通。"
            if body.get("response_format", {}).get("type") == "json_schema":
                text = json.dumps({"answer": 7})
            envelope = {"id": identifier, "created": 0, "model": body["model"]}
            names = {t.get("function", {}).get("name") for t in body.get("tools", [])}
            if {"inspect_document", "submit_review"} <= names:
                inspected = any(m.get("role") == "tool" for m in body["messages"])
                tool = "submit_review" if inspected else "inspect_document"
                args = (
                    {"verdict": "reviewed", "summary": "模拟审查已完成；此结果仅验证流程链路。"}
                    if inspected
                    else {}
                )
                if inspected:
                    source = json.loads(
                        next(
                            m["content"]
                            for m in body["messages"]
                            if m.get("role") == "tool"
                            and m.get("tool_call_id") == "call-inspect_document"
                        )
                    )
                    args.update(
                        source_file_id=source["source_file_id"], characters=source["characters"]
                    )
                return httpx2.Response(
                    200,
                    json={
                        **envelope,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-" + tool,
                                            "type": "function",
                                            "function": {
                                                "name": tool,
                                                "arguments": json.dumps(args, ensure_ascii=False),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": usage,
                    },
                )
            if body.get("stream"):
                events = [
                    {
                        **envelope,
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": None}
                        ],
                    },
                    {
                        **envelope,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                    {**envelope, "object": "chat.completion.chunk", "choices": [], "usage": usage},
                ]
                chunks = [("data: " + json.dumps(event) + "\n\n").encode() for event in events]
                chunks.append(b"data: [DONE]\n\n")
                return httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=MockStream(chunks)
                )
            return httpx2.Response(
                200,
                json={
                    **envelope,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                },
            )
        if request.method == "GET" and request.url.path.endswith("/generation"):
            return httpx2.Response(
                200, json={"data": {"id": request.url.params["id"], "total_cost": "0.0012"}}
            )
        if request.method == "GET" and request.url.path.endswith("/credits"):
            return httpx2.Response(
                200, json={"data": {"total_credits": "100.00", "total_usage": "12.50"}}
            )
        if request.method == "GET" and request.url.path.endswith("/user/balance"):
            return httpx2.Response(
                200,
                json={
                    "balance_infos": [
                        {
                            "currency": "CNY",
                            "total_balance": "100.00",
                            "granted_balance": "10.00",
                            "topped_up_balance": "90.00",
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path.endswith("/mock/QueryAccountBalance"):
            return httpx2.Response(
                200,
                json={
                    "Data": {
                        "Currency": "CNY",
                        "AvailableAmount": "100.00",
                        "AvailableCashAmount": "80.00",
                    }
                },
            )
        return httpx2.Response(404, json={"error": {"message": "Mock route unavailable"}})

    return httpx2.MockTransport(respond)


def install(monkeypatch=None):
    """Inject deterministic HTTP responses in tests, including isolated child processes."""
    from keystone.platform.model_access.adapters.http import HttpClient
    from keystone.platform.model_access.adapters.inference import client as gateway

    def set_value(obj, name, value):
        if monkeypatch is None:
            setattr(obj, name, value)
        else:
            monkeypatch.setattr(obj, name, value)

    from keystone.platform.model_access.adapters.billing.registry import PROVIDERS

    set_value(PROVIDERS["aliyun_bailian"], "balance_path", "/mock/QueryAccountBalance")
    for provider in ("deepseek", "aliyun_bailian"):
        set_value(PROVIDERS[provider], "enabled", True)
        set_value(PROVIDERS[provider], "api_base", gateway.OPENROUTER_BASE)
    targets = dict(gateway.SUPPORTED_TARGETS)
    for provider in ("deepseek", "aliyun_bailian"):
        targets[provider] = (
            gateway.OPENROUTER_BASE,
            {
                "/chat/completions": "POST",
                "/user/balance": "GET",
                "/mock/QueryAccountBalance": "GET",
            },
        )
    set_value(gateway, "SUPPORTED_TARGETS", targets)
    operations = {
        **gateway.SUPPORTED_OPERATIONS,
        "deepseek": frozenset({"text.generate"}),
        "aliyun_bailian": frozenset({"text.generate"}),
    }
    set_value(gateway, "SUPPORTED_OPERATIONS", operations)
    # entries imports this mapping directly.
    from keystone.platform.model_access.services import entries

    set_value(entries, "SUPPORTED_TARGETS", targets)
    set_value(entries, "SUPPORTED_OPERATIONS", operations)

    async def address(self, hostname):
        return "1.1.1.1"

    set_value(HttpClient, "public_address", address)
    set_value(HttpClient, "transport", lambda self, provider: transport(provider))
