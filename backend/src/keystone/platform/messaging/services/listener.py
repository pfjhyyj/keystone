"""Worker-side long connections: one per active Feishu app channel in every Worker process.

Feishu delivers each event to one random connection of the app, so several Workers need no
coordination. Channel changes are picked up by polling; a changed channel reconnects.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.messaging.adapters import feishu_app, feishu_ws
from keystone.platform.messaging.models import MessageChannel
from keystone.platform.messaging.services import inbound
from keystone.platform.messaging.services.channels import unseal

SYNC_SECONDS = 30
log = logging.getLogger(__name__)


class Listeners:
    def __init__(self, runtime, vault, *, connector=feishu_ws.connect, transport=None):
        self.runtime, self.vault = runtime, vault
        self.connector, self.transport = connector, transport
        # channel ID -> (version, connection, task)
        self.running = {}
        self.replies = set()
        # Shared by all of this Worker's connections.
        self.slots = asyncio.Semaphore(inbound.INLINE_LIMIT)

    async def sync(self):
        async with self.runtime.database.transaction() as session:
            rows = (
                await session.scalars(
                    select(MessageChannel).where(
                        MessageChannel.provider == "feishu_app",
                        MessageChannel.is_active.is_(True),
                    )
                )
            ).all()
        wanted = {}
        for row in rows:
            try:
                wanted[row.id] = (row.updated_at, row.app_id, unseal(self.vault, row)["app_secret"])
            except ServiceError:
                log.warning("messaging_listener_credential_unavailable channel=%s", row.id)
        for channel_id, (version, connection, _) in list(self.running.items()):
            if wanted.get(channel_id, (None,))[0] != version:
                connection.stop()
                await self._forget(channel_id)
        for channel_id, (version, app_id, secret) in wanted.items():
            if channel_id not in self.running:
                self._start(channel_id, version, app_id, secret)

    def _start(self, channel_id, version, app_id, secret):
        channel = {"id": channel_id, "app_id": app_id}

        def reply(content, open_id, key):
            task = asyncio.create_task(self._reply(app_id, secret, content, open_id, key))
            self.replies.add(task)
            task.add_done_callback(self.replies.discard)

        async def handle(payload):
            return await inbound.receive(self.runtime, channel, payload, reply, self.slots)

        connection = feishu_ws.Connection(
            app_id,
            secret,
            handle,
            label=str(channel_id),
            transport=self.transport,
            connector=self.connector,
        )
        self.running[channel_id] = (version, connection, asyncio.create_task(connection.run()))

    async def _reply(self, app_id, secret, content, open_id, key):
        try:
            # The message ID keys the send, so a redelivered message is answered once.
            await feishu_app.send(app_id, secret, "open_id", open_id, content, key[:50])
        except (feishu_app.Rejected, feishu_app.NotSent, feishu_app.Unknown):
            log.warning("messaging_bind_reply_failed")

    async def _forget(self, channel_id):
        _, connection, task = self.running.pop(channel_id)
        connection.stop()
        await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        for channel_id in list(self.running):
            await self._forget(channel_id)
        await asyncio.gather(*self.replies, return_exceptions=True)

    async def run(self, stop):
        try:
            while not stop.is_set():
                try:
                    await self.sync()
                except Exception as error:
                    log.warning("messaging_listener_sync_failed type=%s", type(error).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=SYNC_SECONDS)
                except TimeoutError:
                    pass
        finally:
            await self.close()


@asynccontextmanager
async def listening(runtime):
    """Keep app connections open while the Worker runs; close them all on the way out."""
    stop = asyncio.Event()
    listeners = Listeners(runtime, runtime.messaging_vault())
    task = asyncio.create_task(listeners.run(stop))
    try:
        yield listeners
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
