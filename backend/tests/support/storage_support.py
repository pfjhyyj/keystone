"""Small local S3 protocol fixture; exercises the real OpenDAL S3 client."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from keystone.core.storage_config import StorageConfig


@contextmanager
def s3_endpoint():
    state = SimpleNamespace(objects={}, calls=[], fail_put=False, unavailable=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, data=b"", *, size=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(data) if size is None else size))
            self.send_header("Content-Type", "application/xml")
            self.send_header("ETag", '"fixture-etag"')
            self.send_header("Last-Modified", "Mon, 19 Oct 2026 00:00:00 GMT")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def dispatch(self):
            path = unquote(urlsplit(self.path).path)
            state.calls.append((self.command, path, self.headers.get("Authorization", "")))
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if state.unavailable:
                self.respond(503, b"<Error><Code>ServiceUnavailable</Code></Error>")
            elif self.command == "PUT":
                state.objects[path] = body
                if state.fail_put:
                    self.respond(500, b"<Error><Code>InternalError</Code></Error>")
                else:
                    self.respond(200)
            elif self.command == "DELETE":
                state.objects.pop(path, None)
                self.respond(204)
            elif path in state.objects:
                data = state.objects[path]
                self.respond(200, data, size=len(data))
            elif path.rstrip("/") == "/backups":
                self.respond(
                    200,
                    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/'
                    b'2006-03-01/"><Name>backups</Name><Prefix></Prefix>'
                    b"<KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>"
                    b"<IsTruncated>false</IsTruncated></ListBucketResult>",
                )
            else:
                self.respond(404, b"<Error><Code>NoSuchKey</Code></Error>")

        do_PUT = do_GET = do_HEAD = do_DELETE = dispatch

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def s3():
    with s3_endpoint() as state:
        yield state


def cloud_config(s3, **changes):
    return StorageConfig(
        driver="s3",
        endpoint=s3.endpoint,
        bucket="backups",
        prefix="business-a",
        access_key="fixture-access",
        secret_key="fixture-secret",
        **changes,
    )
