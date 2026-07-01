import asyncio
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from config import API_HASH, APP_ID, AUTO_REPOST_ENABLED, LOGGER
from premium.storage import (
    get_userbot_session,
    complete_queued_repost,
    enqueue_repost,
    get_due_repost_pairs,
    is_auto_repost_enabled,
    list_repost_pairs,
    mark_repost_error,
    mark_repost_processed,
)


logger = LOGGER(__name__)


class AutoRepostWorker:
    def __init__(self):
        self.client = None
        self._lock = asyncio.Lock()
        self.queue_task = None

    @property
    def connected(self):
        return bool(self.client and self.client.is_connected)

    async def requirements(self):
        session = await get_userbot_session()
        pairs = await list_repost_pairs(active_only=True)
        enabled = await is_auto_repost_enabled(AUTO_REPOST_ENABLED)
        return {
            "session": session,
            "pairs": pairs,
            "enabled": enabled,
            "ready": bool(session and pairs and enabled),
        }

    async def start(self):
        async with self._lock:
            if self.connected:
                return True
            state = await self.requirements()
            if not state["ready"]:
                logger.info(
                    "Auto repost idle: enabled=%s session=%s pairs=%s",
                    state["enabled"],
                    bool(state["session"]),
                    len(state["pairs"]),
                )
                return False
            client = Client(
                "premium_auto_repost",
                api_id=APP_ID,
                api_hash=API_HASH,
                session_string=state["session"],
                in_memory=True,
                sleep_threshold=120,
            )
            client.add_handler(MessageHandler(self._on_channel_post, filters.channel))
            try:
                await client.start()
            except Exception:
                logger.exception("Auto repost userbot failed to start")
                if client.is_connected:
                    await client.stop()
                raise
            self.client = client
            self.queue_task = asyncio.create_task(self._queue_loop())
            logger.info("Auto repost userbot connected")
            return True

    async def stop(self):
        async with self._lock:
            client, self.client = self.client, None
            task, self.queue_task = self.queue_task, None
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if client and client.is_connected:
                await client.stop()
            logger.info("Auto repost userbot stopped")

    async def restart(self):
        await self.stop()
        return await self.start()

    async def _on_channel_post(self, client, message):
        pairs = await list_repost_pairs(active_only=True)
        targets = [item for item in pairs if int(item["source"]) == int(message.chat.id)]
        for pair in targets:
            source, target = int(pair["source"]), int(pair["target"])
            interval = int(pair.get("interval_hours") or 0)
            if interval > 0:
                await enqueue_repost(source, target, message.id)
                logger.info(
                    "Queued source=%s message=%s target=%s interval=%sh",
                    source,
                    message.id,
                    target,
                    interval,
                )
                continue
            try:
                await message.copy(target)
                await mark_repost_processed(source, target)
                logger.info("Reposted source=%s message=%s target=%s", source, message.id, target)
            except Exception as exc:
                await mark_repost_error(source, target, exc)
                logger.exception("Repost failed source=%s target=%s", source, target)

    async def _queue_loop(self):
        while True:
            try:
                await self._process_due_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled repost queue failed")
            await asyncio.sleep(30)

    async def _process_due_queue(self):
        now = datetime.now(timezone.utc)
        for pair in await get_due_repost_pairs(now):
            pending = pair.get("pending_message_ids") or []
            if not pending:
                continue
            source, target = int(pair["source"]), int(pair["target"])
            message_id = int(pending[0])
            interval = int(pair.get("interval_hours") or 0)
            try:
                message = await self.client.get_messages(source, message_id)
                if not message or getattr(message, "empty", False):
                    raise RuntimeError(f"Source message {message_id} is unavailable")
                await message.copy(target)
                await complete_queued_repost(source, target, message_id, interval)
                logger.info(
                    "Scheduled repost completed source=%s message=%s target=%s next=%sh",
                    source,
                    message_id,
                    target,
                    interval,
                )
            except Exception as exc:
                await mark_repost_error(source, target, exc)
                logger.exception("Scheduled repost failed source=%s target=%s", source, target)
