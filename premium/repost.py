import asyncio

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from config import API_HASH, APP_ID, AUTO_REPOST_ENABLED, LOGGER
from premium.storage import (
    get_userbot_session,
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
            logger.info("Auto repost userbot connected")
            return True

    async def stop(self):
        async with self._lock:
            client, self.client = self.client, None
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
            try:
                await message.copy(target)
                await mark_repost_processed(source, target)
                logger.info("Reposted source=%s message=%s target=%s", source, message.id, target)
            except Exception as exc:
                await mark_repost_error(source, target, exc)
                logger.exception("Repost failed source=%s target=%s", source, target)
