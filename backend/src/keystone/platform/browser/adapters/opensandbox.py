"""Fixed OpenSandbox 1.2 HTTP and WebSocket endpoints."""

import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx2
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


class RuntimeUnavailable(Exception):
    """An outcome may be unknown. Never replay a create after this error."""


class CreateRejected(Exception):
    """The runtime refused the request before provisioning."""


class OpenSandbox:
    def __init__(self, url, key, image):
        self.url, self.image = url.rstrip("/"), image
        self.headers = {"OPEN-SANDBOX-API-KEY": key}
        self.http = httpx2.AsyncClient(
            base_url=self.url, headers=self.headers, timeout=8, trust_env=False
        )

    def _control_headers(self, run_id):
        key = self.headers["OPEN-SANDBOX-API-KEY"].encode()
        return {
            "X-Keystone-Browser-Control": hmac.digest(key, str(run_id).encode(), "sha256").hex(),
            "X-EXECD-ACCESS-TOKEN": hmac.digest(key, f"execd:{run_id}".encode(), "sha256").hex(),
        }

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise RuntimeUnavailable
        return value

    async def _request(self, method, path, **kwargs):
        try:
            return await self.http.request(method, path, **kwargs)
        except httpx2.HTTPError:
            raise RuntimeUnavailable from None

    @staticmethod
    def _data(response):
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx2.HTTPError, ValueError):
            raise RuntimeUnavailable from None
        if not isinstance(data, dict):
            raise RuntimeUnavailable
        return data

    async def create(self, run_id, workspace_id, volume, *, purpose="browse"):
        control = self._control_headers(run_id)
        response = await self._request(
            "POST",
            "/v1/sandboxes",
            timeout=18,
            json={
                "image": {"uri": self.image},
                "entrypoint": ["/usr/local/bin/browser-runtime"]
                + (["--maintenance"] if purpose == "backup" else []),
                "env": {
                    "KEYSTONE_BROWSER_CONTROL": control["X-Keystone-Browser-Control"],
                    "EXECD_ACCESS_TOKEN": control["X-EXECD-ACCESS-TOKEN"],
                },
                "timeout": None,
                "resourceLimits": {"cpu": "2", "memory": "2Gi"},
                "metadata": {
                    "keystone_run": str(run_id),
                    "keystone_workspace": str(workspace_id),
                },
                "volumes": [
                    {
                        "name": "browser-profile",
                        "pvc": {"claimName": volume, "deleteOnSandboxTermination": False},
                        "mountPath": "/data/profile",
                        "readOnly": False,
                    }
                ],
            },
        )
        if response.status_code in {401, 403, 422}:
            raise CreateRejected
        return self._id(self._data(response).get("id"))

    async def find(self, run_id, workspace_id):
        response = await self._request(
            "GET",
            "/v1/sandboxes",
            params={
                "metadata": f"keystone_run={run_id}&keystone_workspace={workspace_id}",
                "pageSize": 2,
            },
        )
        data = self._data(response)
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 1:
            raise RuntimeUnavailable
        if not items:
            return None
        item = items[0]
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict) or (
            metadata.get("keystone_run") != str(run_id)
            or metadata.get("keystone_workspace") != str(workspace_id)
        ):
            raise RuntimeUnavailable
        return self._id(item.get("id"))

    async def exists(self, sandbox_id):
        response = await self._request("GET", f"/v1/sandboxes/{self._id(sandbox_id)}")
        if response.status_code == 404:
            return False
        self._data(response)
        return True

    def _proxy(self, sandbox_id, port, suffix):
        # Proxy to execd on the sandbox network; the two runtime services only listen on loopback.
        return f"/v1/sandboxes/{self._id(sandbox_id)}/proxy/44772/proxy/{port}/{suffix}"

    async def ready(self, sandbox_id, run_id):
        response = await self._request(
            "GET",
            self._proxy(sandbox_id, 6081, "health"),
            headers=self._control_headers(run_id),
        )
        if response.status_code != 200:
            return False
        return self._data(response).get("status") == "ready"

    async def wait_ready(self, sandbox_id, run_id):
        async with asyncio.timeout(8):
            while not await self.ready(sandbox_id, run_id):
                await asyncio.sleep(0.4)

    async def wait_profile(self, sandbox_id, run_id):
        async with asyncio.timeout(8):
            while True:
                response = await self._request(
                    "GET",
                    self._proxy(sandbox_id, 6081, "profile"),
                    headers=self._control_headers(run_id),
                )
                if response.status_code == 200:
                    state = self._data(response).get("status")
                    if state in {"present", "missing", "invalid"}:
                        return state
                    raise RuntimeUnavailable
                await asyncio.sleep(0.4)

    @asynccontextmanager
    async def backup(self, sandbox_id, run_id, key, limit):
        headers = {
            **self._control_headers(run_id),
            "X-Keystone-Browser-Backup-Key": key,
            "X-Keystone-Browser-Backup-Limit": str(limit),
        }
        try:
            async with self.http.stream(
                "GET", self._proxy(sandbox_id, 6081, "backup"), headers=headers, timeout=120
            ) as response:
                response.raise_for_status()
                size = int(response.headers.get("Content-Length", "0"))
                digest = response.headers.get("X-Keystone-Browser-Checksum", "")
                if not 33 < size <= limit or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise RuntimeUnavailable
                yield size, digest, response.aiter_bytes(1024 * 1024)
        except (ValueError, httpx2.HTTPError):
            raise RuntimeUnavailable from None

    async def restore(self, sandbox_id, run_id, key, size, chunks):
        response = await self._request(
            "POST",
            self._proxy(sandbox_id, 6081, "restore"),
            headers={
                **self._control_headers(run_id),
                "X-Keystone-Browser-Backup-Key": key,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
            content=chunks,
            timeout=120,
        )
        if response.status_code == 422:
            return False
        if response.status_code != 204:
            raise RuntimeUnavailable
        return True

    async def quit_browser(self, sandbox_id, run_id):
        response = await self._request(
            "POST",
            self._proxy(sandbox_id, 6081, "stop"),
            headers=self._control_headers(run_id),
        )
        if response.status_code != 204:
            raise RuntimeUnavailable

    async def delete(self, sandbox_id):
        response = await self._request("DELETE", f"/v1/sandboxes/{self._id(sandbox_id)}")
        if response.status_code not in {200, 202, 204, 404}:
            raise RuntimeUnavailable
        async with asyncio.timeout(5):
            while await self.exists(sandbox_id):
                await asyncio.sleep(0.25)

    @asynccontextmanager
    async def stream(self, sandbox_id, run_id):
        url = urlsplit(self.url)
        ws = urlunsplit(
            (
                "wss" if url.scheme == "https" else "ws",
                url.netloc,
                self._proxy(sandbox_id, 6080, ""),
                "",
                "",
            )
        )
        try:
            async with connect(
                ws,
                additional_headers={**self.headers, **self._control_headers(run_id)},
                open_timeout=3,
                close_timeout=1,
                max_size=8 * 1024 * 1024,
                proxy=None,
                subprotocols=["binary"],
            ) as socket:
                yield socket
        except (OSError, TimeoutError, WebSocketException):
            raise RuntimeUnavailable from None
