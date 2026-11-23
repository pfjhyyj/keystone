"""Worker-side long connections: one per active Feishu app channel in every Worker process.

Feishu delivers each event to one random connection of the app, so several Workers need no
coordination. Channel changes are picked up by polling; a changed channel reconnects.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.platform.messaging.adapters import feishu_app, feishu_ws
from keystone.platform.messaging.models import MessageChannel, MessageConnection
from keystone.platform.messaging.services import inbound
from keystone.platform.messaging.services.channels import unseal

# Readers treat connection rows older than three syncs as gone (channels.CONNECTION_FRESH).
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
        self.worker_id = runtime.settings.worker_id
        self.writes = set()

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
        await self._report()

    async def _report(self, only=None):
        """Record this Worker's connection states; rows for channels it dropped go away."""
        running = {
            channel_id: connection
            for channel_id, (_, connection, _) in self.running.items()
            if only is None or channel_id == only
        }
        async with self.runtime.database.transaction() as session:
            if only is None:
                await session.execute(
                    delete(MessageConnection).where(
                        MessageConnection.worker_id == self.worker_id,
                        MessageConnection.channel_id.not_in(list(self.running)),
                    )
                )
            for channel_id, connection in running.items():
                values = {
                    "state": connection.state,
                    "error_code": connection.error_code,
                    "updated_at": func.now(),
                }
                await session.execute(
                    insert(MessageConnection)
                    .values(channel_id=channel_id, worker_id=self.worker_id, **values)
                    .on_conflict_do_update(
                        index_elements=[MessageConnection.channel_id, MessageConnection.worker_id],
                        set_=values,
                    )
                )

    def _changed(self, channel_id):
        async def write():
            try:
                await self._report(only=channel_id)
            except Exception as error:
                # The next sync writes the state again.
                log.warning("messaging_listener_report_failed type=%s", type(error).__name__)

        task = asyncio.create_task(write())
        self.writes.add(task)
        task.add_done_callback(self.writes.discard)

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
            on_change=lambda: self._changed(channel_id),
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
        await asyncio.gather(*self.replies, *self.writes, return_exceptions=True)
        try:
            await self._report()
        except Exception:
            log.warning("messaging_listener_report_failed type=close")

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
