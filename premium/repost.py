import asyncio
import re
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from config import API_HASH, APP_ID, AUTO_REPOST_ENABLED, LOGGER
from premium.conversion import convert_post, extract_deeplinks, publish_converted
from premium.storage import (
    advance_backfill,
    get_userbot_session,
    complete_queued_repost,
    discard_queued_repost,
    enqueue_repost,
    get_due_repost_pairs,
    is_auto_repost_enabled,
    list_repost_pairs,
    mark_repost_error,
    mark_repost_processed,
)


logger = LOGGER(__name__)

DEEPLINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/[A-Za-z0-9_]+\?start=[A-Za-z0-9_\-=]+"
    r"|@[A-Za-z0-9_]+\?start=[A-Za-z0-9_\-=]+",
    re.IGNORECASE,
)


def message_text(message):
    text = getattr(message, "caption", None) or getattr(message, "text", None) or ""
    return text.html if hasattr(text, "html") else str(text)


def has_deeplink(message):
    return bool(extract_deeplinks(message))


class AutoRepostWorker:
    def __init__(self, bot):
        self.bot = bot
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
                dialog_count = 0
                async for _ in client.get_dialogs():
                    dialog_count += 1
                logger.info("Auto repost peer cache hydrated from %s dialogs", dialog_count)
                for pair in state["pairs"]:
                    source, target = int(pair["source"]), int(pair["target"])
                    try:
                        await client.get_chat(source)
                        await client.get_chat(target)
                        logger.info("Auto repost pair ready source=%s target=%s", source, target)
                    except Exception as exc:
                        await mark_repost_error(source, target, exc)
                        logger.error(
                            "Auto repost pair inaccessible source=%s target=%s: %s",
                            source,
                            target,
                            exc,
                        )
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
        if targets and not has_deeplink(message):
            logger.info(
                "Auto repost skipped source=%s message=%s: no deeplink",
                message.chat.id,
                message.id,
            )
            return
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
                converted = await self._convert_required(message, target)
                if not converted:
                    continue
                await publish_converted(client, target, message, converted)
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
            source, target = int(pair["source"]), int(pair["target"])
            interval = int(pair.get("interval_hours") or 0)
            if pair.get("backfill_active"):
                await self._process_backfill(pair, source, target, interval)
                continue
            if not pending:
                continue
            message_id = int(pending[0])
            try:
                message = await self.client.get_messages(source, message_id)
                if not message or getattr(message, "empty", False):
                    raise RuntimeError(f"Source message {message_id} is unavailable")
                if not has_deeplink(message):
                    await discard_queued_repost(source, target, message_id, "skipped: no deeplink")
                    logger.info(
                        "Scheduled repost skipped source=%s message=%s: no deeplink",
                        source,
                        message_id,
                    )
                    continue
                converted = await self._convert_required(message, target)
                if not converted:
                    await discard_queued_repost(source, target, message_id, "conversion failed; send blocked")
                    continue
                await publish_converted(self.client, target, message, converted)
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

    async def _process_backfill(self, pair, source, target, interval):
        cursor = int(pair.get("backfill_cursor") or 1)
        end = int(pair.get("backfill_end") or 0)
        while cursor <= end:
            message = await self.client.get_messages(source, cursor)
            if (
                message
                and not getattr(message, "empty", False)
                and not getattr(message, "service", None)
                and has_deeplink(message)
            ):
                try:
                    converted = await self._convert_required(message, target)
                    if not converted:
                        await advance_backfill(source, target, cursor + 1, end, interval, False)
                        return
                    await publish_converted(self.client, target, message, converted)
                    await advance_backfill(source, target, cursor + 1, end, interval, True)
                    logger.info(
                        "Backfill repost completed source=%s message=%s target=%s next=%sh",
                        source,
                        cursor,
                        target,
                        interval,
                    )
                except Exception as exc:
                    await mark_repost_error(source, target, exc)
                    logger.exception("Backfill repost failed source=%s target=%s", source, target)
                return
            cursor += 1
        await advance_backfill(source, target, cursor, end, interval, False)
        logger.info("Backfill completed source=%s target=%s", source, target)

    async def _convert_required(self, message, target):
        session_found = bool(await get_userbot_session())
        selected_bot = getattr(self.bot, "username", None) or "unknown"
        base = (
            "source_message_id=%s target_channel_id=%s selected_repost_bot=%s "
            "session_found=%s"
        )
        if not session_found:
            reason = "Deeplink conversion skipped because this repost bot has no per-bot userbot session."
            logger.error(
                base + " deeplink_conversion_started=false deeplink_conversion_success=false send_blocked=true reason=%s",
                message.id,
                target,
                selected_bot,
                False,
                reason,
            )
            return None
        logger.info(
            base + " deeplink_conversion_started=true deeplink_conversion_success=false send_blocked=true reason=conversion_started",
            message.id,
            target,
            selected_bot,
            True,
        )
        try:
            converted = await convert_post(self.bot, self.client, message, logger)
        except Exception as exc:
            logger.exception(
                base + " deeplink_conversion_started=true deeplink_conversion_success=false send_blocked=true reason=%s",
                message.id,
                target,
                selected_bot,
                True,
                exc,
            )
            return None
        success = bool(converted)
        logger.info(
            base + " deeplink_conversion_started=true deeplink_conversion_success=%s send_blocked=%s reason=%s",
            message.id,
            target,
            selected_bot,
            True,
            success,
            not success,
            "conversion_complete" if success else "conversion_failed",
        )
        return converted
