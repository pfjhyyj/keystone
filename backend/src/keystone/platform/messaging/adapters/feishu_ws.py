"""Feishu long connection: the Worker connects out and receives app events.

The protocol has no public specification; this follows the official SDK (lark-oapi 1.8.0,
lark_oapi/ws/client.py). The SDK itself is not used: it runs a module-level event loop,
blocks forever in start(), cannot be stopped, calls handlers synchronously and logs event
bodies at debug level.

1. POST /callback/ws/endpoint with AppID/AppSecret returns the socket URL and timings.
2. Frames are protobuf `pbbp2.Frame`; method 0 is control (ping/pong), 1 is data.
3. A data frame is answered by writing it back with payload `{"code": 200, "data": b64}`.

Event bodies, app secrets and connection URLs (they carry a ticket) are never logged.
https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case
"""

import asyncio
import base64
import json
import logging
import random
import time
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from keystone.platform.messaging.adapters.feishu_http import Unknown, call

ENDPOINT_URL = "https://open.feishu.cn/callback/ws/endpoint"
MAX_EVENT = 1024 * 1024
# Parts of one split event must all arrive within this many seconds.
PART_SECONDS = 5
# The platform waits 3 seconds; answering later only causes a redelivery.
HANDLER_SECONDS = 2.5
# Feishu replaces these defaults on every handshake and in pong frames.
DEFAULTS = {"PingInterval": 120, "ReconnectInterval": 120, "ReconnectNonce": 30}

log = logging.getLogger(__name__)


class EndpointRefused(Exception):
    """The platform refused to hand out a connection (bad credentials, too many clients)."""

    def __init__(self, code):
        self.code = code


# --- pbbp2.Frame: SeqID=1 LogID=2 (uint64), service=3 method=4 (int32),
# headers=5 (repeated Header{key=1, value=2}), payload_encoding=6 payload_type=7 (string),
# payload=8 (bytes), LogIDNew=9 (string).
INTS = {1: "seq_id", 2: "log_id", 3: "service", 4: "method"}
STRINGS = {6: "payload_encoding", 7: "payload_type", 9: "log_id_new"}


def _varint(value):
    out = bytearray()
    value &= (1 << 64) - 1  # int32 fields encode negatives as ten-byte varints
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data, index):
    result = shift = 0
    while True:
        if index >= len(data) or shift > 63:
            raise ValueError("Truncated varint")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7


def _fields(data):
    index = 0
    while index < len(data):
        tag, index = _read_varint(data, index)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, index = _read_varint(data, index)
        elif wire == 2:
            size, index = _read_varint(data, index)
            if index + size > len(data):
                raise ValueError("Truncated field")
            value, index = data[index : index + size], index + size
        elif wire == 1:
            value, index = data[index : index + 8], index + 8
        elif wire == 5:
            value, index = data[index : index + 4], index + 4
        else:
            raise ValueError("Unsupported wire type")
        yield number, wire, value


def _bytes_field(number, value):
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def encode_frame(frame):
    out = bytearray()
    for number, name in INTS.items():
        out += _varint(number << 3) + _varint(frame.get(name, 0))
    for key, value in frame.get("headers", []):
        header = _bytes_field(1, key.encode()) + _bytes_field(2, value.encode())
        out += _bytes_field(5, header)
    # Field-number order, as protobuf writes it.
    for number, name, value in (
        (6, "payload_encoding", None),
        (7, "payload_type", None),
        (8, "payload", frame.get("payload")),
        (9, "log_id_new", None),
    ):
        # Set but empty strings are written too, as protobuf does for proto2 optional fields.
        if number != 8 and frame.get(name) is not None:
            value = frame[name].encode()
        if value is not None:
            out += _bytes_field(number, value)
    return bytes(out)


def decode_frame(data):
    frame = {"headers": []}
    for number, wire, value in _fields(data):
        if number in INTS and wire == 0:
            # int32 fields arrive sign-extended to 64 bits.
            if number in {3, 4} and value >= 1 << 63:
                value -= 1 << 64
            frame[INTS[number]] = value
        elif number == 5 and wire == 2:
            header = {key: item for key, _, item in _fields(value)}
            frame["headers"].append(
                (bytes(header.get(1, b"")).decode(), bytes(header.get(2, b"")).decode())
            )
        elif number in STRINGS and wire == 2:
            frame[STRINGS[number]] = bytes(value).decode()
        elif number == 8 and wire == 2:
            frame["payload"] = bytes(value)
    return frame


def header(frame, key):
    return next((value for name, value in frame["headers"] if name == key), None)


# --- connection


async def endpoint(app_id, secret, *, transport=None):
    """Return (socket URL, timings); the URL holds a short-lived ticket and is never logged."""
    status, answer = await call(
        "POST",
        ENDPOINT_URL,
        transport=transport,
        headers={"locale": "zh"},
        json={"AppID": app_id, "AppSecret": secret},
    )
    code = answer.get("code") if answer else None
    data = (answer or {}).get("data") or {}
    if status >= 500 or answer is None or code == 1:
        raise Unknown
    if code != 0 or not isinstance(data.get("URL"), str):
        raise EndpointRefused(f"feishu_ws_{code}" if type(code) is int else "provider_rejected")
    return data["URL"], _timings(data.get("ClientConfig"))


def _timings(config, current=None):
    result = dict(current or DEFAULTS)
    for key in DEFAULTS:
        value = (config or {}).get(key)
        if type(value) is int and 0 < value <= 3600:
            result[key] = value
    return result


def _answer(frame, result, elapsed_ms):
    body = {"code": 200}
    if result is not None:
        body["data"] = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
    return {
        **frame,
        "headers": [*frame["headers"], ("biz_rt", str(elapsed_ms))],
        "payload": json.dumps(body).encode(),
    }


class Connection:
    """One app's connection, reconnecting until stop() or a refused endpoint."""

    def __init__(self, app_id, secret, handle, *, label, transport=None, connector=connect):
        self.app_id, self.secret, self.handle = app_id, secret, handle
        self.label, self.transport, self.connector = label, transport, connector
        self.timings = dict(DEFAULTS)
        self.connected = False
        self._stop = asyncio.Event()
        self._parts = {}

    def stop(self):
        self._stop.set()

    async def run(self):
        first = True
        while not self._stop.is_set():
            if not first:
                # Spread reconnects so a restart does not reconnect every Worker at once.
                await self._sleep(
                    self.timings["ReconnectInterval"]
                    + random.random() * self.timings["ReconnectNonce"]
                )
                if self._stop.is_set():
                    return
            first = False
            try:
                url, self.timings = await endpoint(
                    self.app_id, self.secret, transport=self.transport
                )
                await self._serve(url)
            except EndpointRefused as error:
                log.warning("messaging_listener_refused channel=%s code=%s", self.label, error.code)
            except (Unknown, OSError, WebSocketException, ValueError, KeyError) as error:
                log.warning(
                    "messaging_listener_lost channel=%s type=%s", self.label, type(error).__name__
                )
            finally:
                self.connected = False

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _serve(self, url):
        service = int(parse_qs(urlparse(url).query)["service_id"][0])
        async with self.connector(url, proxy=None, max_size=MAX_EVENT) as socket:
            self.connected = True
            log.info("messaging_listener_connected channel=%s", self.label)
            pending = set()
            stopping = asyncio.create_task(self._stop.wait())
            pinging = asyncio.create_task(self._ping(socket, service))
            try:
                while True:
                    receiving = asyncio.create_task(socket.recv())
                    done, _ = await asyncio.wait(
                        {receiving, stopping, pinging}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if receiving not in done:
                        receiving.cancel()
                        if pinging in done:
                            pinging.result()
                        return
                    data = receiving.result()
                    if isinstance(data, bytes):
                        task = asyncio.create_task(self._frame(socket, data))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
            finally:
                for task in (stopping, pinging, *pending):
                    task.cancel()
                await asyncio.gather(stopping, pinging, *pending, return_exceptions=True)

    async def _ping(self, socket, service):
        while True:
            await socket.send(
                encode_frame({"service": service, "method": 0, "headers": [("type", "ping")]})
            )
            await asyncio.sleep(self.timings["PingInterval"])

    async def _frame(self, socket, data):
        try:
            frame = decode_frame(data)
        except ValueError:
            log.warning("messaging_listener_bad_frame channel=%s", self.label)
            return
        kind = header(frame, "type")
        if frame.get("method") == 0:
            if kind == "pong" and frame.get("payload"):
                try:
                    self.timings = _timings(json.loads(frame["payload"]), self.timings)
                except ValueError:
                    pass
            return
        # The SDK leaves legacy "card" frames unanswered; only "event" frames carry events.
        if kind != "event":
            return
        payload = self._combine(frame)
        if payload is None:
            return
        started = time.monotonic()
        try:
            async with asyncio.timeout(HANDLER_SECONDS):
                result = await self.handle(payload)
        except Exception as error:
            # Unanswered or failed events are pushed again; nothing about them is logged.
            log.warning(
                "messaging_listener_handler_failed channel=%s type=%s",
                self.label,
                type(error).__name__,
            )
            return
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            await socket.send(encode_frame(_answer(frame, result, elapsed)))
        except Exception as error:
            # Never lost silently: an unanswered event times out on the user's side.
            log.warning(
                "messaging_listener_answer_failed channel=%s type=%s",
                self.label,
                type(error).__name__,
            )
            return
        # IDs and timing only, so a missing or late answer can be traced with Feishu.
        log.info(
            "messaging_listener_answered channel=%s message_id=%s trace_id=%s ms=%s body=%s",
            self.label,
            header(frame, "message_id"),
            header(frame, "trace_id"),
            elapsed,
            result is not None,
        )

    def _combine(self, frame):
        total = int(header(frame, "sum") or 1)
        if total <= 1:
            return frame.get("payload") or b""
        message_id, seq = header(frame, "message_id"), int(header(frame, "seq") or 0)
        now = time.monotonic()
        self._parts = {
            key: value for key, value in self._parts.items() if value[0] > now - PART_SECONDS
        }
        if not 0 <= seq < total <= 64:
            return None
        _, parts = self._parts.setdefault(message_id, (now, [None] * total))
        if len(parts) != total:
            return None
        parts[seq] = frame.get("payload") or b""
        if any(part is None for part in parts) or sum(map(len, parts)) > MAX_EVENT:
            return None
        del self._parts[message_id]
        return b"".join(parts)
