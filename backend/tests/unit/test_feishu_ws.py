import asyncio
import base64
import json

import httpx2
import pytest
from websockets.asyncio.server import serve

from keystone.platform.messaging.adapters import feishu_ws
from keystone.platform.messaging.adapters.feishu_ws import Connection, decode_frame, encode_frame

APP, SECRET = "cli_a1b2c3", "app-secret-value"
# Serialized by lark-oapi 1.8.0's own pbbp2.Frame, the reference for this undocumented protocol.
SDK_PING = "08001000180720002a0c0a0474797065120470696e67"
SDK_EVENT = (
    "08051009180720012a0d0a047479706512056576656e742a100a0a6d6573736167655f696412026d312a080a"
    "0373756d1201312a080a037365711201302a0e0a0874726163655f696412027431420b7b2261223a22e4b8ad"
    "227d4a0178"
)

SDK_EMPTY_STRINGS = (
    "08031004180720012a0d0a047479706512056576656e742a100a0a6d6573736167655f696412026d392a080a"
    "0373756d1201312a080a037365711201302a0e0a0874726163655f69641202743932003a0042027b7d4a00"
)


def test_frames_match_the_official_sdk_encoding():
    ping = encode_frame({"service": 7, "method": 0, "headers": [("type", "ping")]})
    assert ping.hex() == SDK_PING
    event = decode_frame(bytes.fromhex(SDK_EVENT))
    assert event["service"] == 7 and event["method"] == 1 and event["seq_id"] == 5
    assert feishu_ws.header(event, "message_id") == "m1"
    assert json.loads(event["payload"]) == {"a": "中"}
    assert encode_frame(event).hex() == SDK_EVENT
    # Feishu sets the optional string fields to empty strings; they must survive the answer.
    empty = decode_frame(bytes.fromhex(SDK_EMPTY_STRINGS))
    assert empty["payload_encoding"] == empty["payload_type"] == empty["log_id_new"] == ""
    assert encode_frame(empty).hex() == SDK_EMPTY_STRINGS
    answer = decode_frame(encode_frame(feishu_ws._answer(empty, {"toast": {}}, 3)))
    assert answer["payload_type"] == "" and feishu_ws.header(answer, "biz_rt") == "3"
    negative = decode_frame(encode_frame({"service": -1, "method": 1, "headers": []}))
    assert negative["service"] == -1


@pytest.mark.parametrize("data", [b"\x08", b"\x2a\x05ab", b"\x0f"])
def test_malformed_frames_are_rejected(data):
    with pytest.raises(ValueError):
        decode_frame(data)


def event_frame(payload, *, message_id="m1", seq=0, total=1):
    return encode_frame(
        {
            "seq_id": 1,
            "service": 7,
            "method": 1,
            "headers": [
                ("type", "event"),
                ("message_id", message_id),
                ("sum", str(total)),
                ("seq", str(seq)),
                ("trace_id", "t"),
            ],
            "payload": payload,
        }
    )


def endpoint_transport(port, seen, code=0):
    def answer(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "code": code,
                "msg": "",
                "data": {
                    "URL": f"ws://127.0.0.1:{port}/ws?device_id=d1&service_id=7",
                    "ClientConfig": {"PingInterval": 1, "ReconnectInterval": 1},
                },
            },
        )

    return httpx2.MockTransport(answer)


def test_events_are_answered_on_the_same_frame_and_split_events_are_joined():
    async def scenario():
        frames, handled, answers = [], [], asyncio.Queue()

        async def server(socket):
            # One whole event, then one split in two parts out of order.
            await socket.send(event_frame(b'{"n":1}'))
            await socket.send(event_frame(b"2}", message_id="m2", seq=1, total=2))
            await socket.send(event_frame(b'{"n":', message_id="m2", seq=0, total=2))
            async for message in socket:
                frame = decode_frame(message)
                frames.append(frame)
                if frame["method"] == 1:
                    await answers.put(frame)

        async def handle(payload):
            handled.append(json.loads(payload))
            return {"toast": {"content": "好"}} if len(handled) == 1 else None

        async with serve(server, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            seen = []
            connection = Connection(
                APP, SECRET, handle, label="c", transport=endpoint_transport(port, seen)
            )
            task = asyncio.create_task(connection.run())
            first, second = [await asyncio.wait_for(answers.get(), 5) for _ in range(2)]
            connection.stop()
            await asyncio.wait_for(task, 5)
        assert seen == [{"AppID": APP, "AppSecret": SECRET}]
        assert handled == [{"n": 1}, {"n": 2}]
        body = json.loads(first["payload"])
        assert body["code"] == 200
        assert json.loads(base64.b64decode(body["data"])) == {"toast": {"content": "好"}}
        assert feishu_ws.header(first, "message_id") == "m1" and first["seq_id"] == 1
        assert feishu_ws.header(first, "biz_rt") is not None
        assert json.loads(second["payload"]) == {"code": 200}
        assert any(frame["method"] == 0 for frame in frames)

    asyncio.run(scenario())


def test_failed_handlers_leave_the_event_unanswered_for_redelivery():
    async def scenario():
        received = asyncio.Event()
        answered = []

        async def server(socket):
            await socket.send(event_frame(b"{}"))
            async for message in socket:
                if decode_frame(message)["method"] == 1:
                    answered.append(message)

        async def handle(payload):
            received.set()
            raise RuntimeError("database unavailable")

        async with serve(server, "127.0.0.1", 0) as listening:
            port = listening.sockets[0].getsockname()[1]
            connection = Connection(
                APP, SECRET, handle, label="c", transport=endpoint_transport(port, [])
            )
            task = asyncio.create_task(connection.run())
            await asyncio.wait_for(received.wait(), 5)
            await asyncio.sleep(0.2)
            connection.stop()
            await asyncio.wait_for(task, 5)
        assert answered == []

    asyncio.run(scenario())


def test_refused_endpoints_are_reported_by_code_and_retried_until_stopped():
    async def scenario():
        calls = []

        def answer(request):
            calls.append(request)
            return httpx2.Response(200, json={"code": 1000040350, "msg": "too many"})

        connection = Connection(
            APP, SECRET, None, label="c", transport=httpx2.MockTransport(answer)
        )
        connection.timings["ReconnectNonce"] = 0
        task = asyncio.create_task(connection.run())
        await asyncio.sleep(0.1)
        with pytest.raises(feishu_ws.EndpointRefused) as error:
            await feishu_ws.endpoint(APP, SECRET, transport=httpx2.MockTransport(answer))
        assert error.value.code == "feishu_ws_1000040350"
        connection.stop()
        await asyncio.wait_for(task, 5)
        assert len(calls) == 2 and not connection.connected

    asyncio.run(scenario())
