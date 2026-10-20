import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import Response
from starlette.responses import StreamingResponse


def byte_range(value, size):
    """Single bytes range, inclusive on the wire; reject multipart and unsatisfiable ranges."""
    if len(value) > 256 or not (match := re.fullmatch(r"bytes=(\d*)-(\d*)", value)):
        raise ValueError("Invalid range")
    first, last = match.groups()
    if not size or not (first or last):
        raise ValueError("Unsatisfiable range")
    if not first:
        count = int(last)
        if not count:
            raise ValueError("Unsatisfiable range")
        return max(0, size - count), size - 1
    start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end


class ContentResponse(Response):
    """Acquire before headers; release even when ASGI send/disconnect cancels iteration."""

    def __init__(self, context, request):
        super().__init__()
        self.context, self.request = context, request
        self._disconnected = False

    @asynccontextmanager
    async def prepare(self, receive):
        # StreamingResponse starts watching disconnect only after headers are ready.
        parent = asyncio.current_task()

        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass
            self._disconnected = True
            parent.cancel()

        watcher = asyncio.create_task(disconnected())
        try:
            async with self.context as reader:
                # Hand receive back to StreamingResponse; never have two consumers.
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                yield reader
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def __call__(self, scope, receive, send):
        try:
            await self._respond(scope, receive, send)
        except asyncio.CancelledError:
            if not self._disconnected:
                raise

    async def _respond(self, scope, receive, send):
        from urllib.parse import quote

        async with self.prepare(receive) as reader:
            disposition = (
                "inline" if reader.content_type.startswith(("video/", "audio/")) else "attachment"
            )
            etag = f'"{reader.checksum}"'
            filename = quote(reader.name, safe="")
            headers = {
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "Cache-Control": "private, no-store",
            }
            start, length, status = 0, reader.size, 200
            value = self.request.headers.get("range")
            if_range = self.request.headers.get("if-range")
            if value and self.request.method == "GET" and (not if_range or if_range == etag):
                try:
                    start, end = byte_range(value, reader.size)
                except ValueError:
                    headers["Content-Range"] = f"bytes */{reader.size}"
                    await Response(status_code=416, headers=headers)(scope, receive, send)
                    return
                length, status = end - start + 1, 206
                headers["Content-Range"] = f"bytes {start}-{end}/{reader.size}"
            headers["Content-Length"] = str(length)
            if self.request.method == "HEAD":
                response = Response(headers=headers, media_type=reader.content_type)
            else:
                response = StreamingResponse(
                    reader.chunks(start, length),
                    status_code=status,
                    headers=headers,
                    media_type=reader.content_type,
                )
            await response(scope, receive, send)
