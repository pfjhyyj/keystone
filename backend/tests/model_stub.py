"""Deterministic upstream simulation, exercised through the real SDK HTTP transport.

This module never constructs a network transport. Mock amounts are test receipts only.
"""

import json

import httpx2


class MockStream(httpx2.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def transport(provider):
    async def respond(request):
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            identifier = "mock-" + request.headers["x-keystone-invocation-id"]
            usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
            if provider == "openrouter":
                usage["cost"] = "0.0012"
            text = "模拟渠道响应：Keystone 模型调用链路已连通。"
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
    from keystone.platform.model_access import gateway

    def set_value(obj, name, value):
        if monkeypatch is None:
            setattr(obj, name, value)
        else:
            monkeypatch.setattr(obj, name, value)

    from keystone.platform.model_access.providers import PROVIDERS

    set_value(PROVIDERS["aliyun_bailian"], "balance_path", "/mock/QueryAccountBalance")
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
    # entries imports this mapping directly.
    from keystone.platform.model_access.services import entries

    set_value(entries, "SUPPORTED_TARGETS", targets)
    set_value(gateway.Gateway, "transport", lambda self, provider: transport(provider))
