import asyncio
from datetime import datetime, timedelta

from database.database import (
    mark_premium_reminder_sent,
    premium_expiry_candidates,
    remove_premium,
)


class PremiumExpiryWorker:
    def __init__(self, bot, interval=900):
        self.bot = bot
        self.interval = interval
        self.task = None

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if not self.task:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None

    async def _run(self):
        while True:
            try:
                await self.run_once()
            except Exception:
                self.bot.LOGGER(__name__).exception("Premium expiry maintenance failed")
            await asyncio.sleep(self.interval)

    async def run_once(self):
        now = datetime.utcnow()
        reminders, expired = await premium_expiry_candidates(now, now + timedelta(hours=24))
        for user in reminders:
            user_id = user["_id"]
            try:
                await self.bot.send_message(
                    user_id,
                    "⚠️ <b>Premium expires soon</b>\n\n"
                    "Your premium expires within 24 hours. Renew now to keep file access.",
                )
                await mark_premium_reminder_sent(user_id)
            except Exception as exc:
                self.bot.LOGGER(__name__).warning("Premium reminder failed for %s: %s", user_id, exc)
        for user in expired:
            user_id = user["_id"]
            await remove_premium(user_id)
            try:
                await self.bot.send_message(
                    user_id,
                    "❌ <b>Premium expired</b>\n\nRenew premium to access protected file links.",
                )
            except Exception as exc:
                self.bot.LOGGER(__name__).warning("Premium expiry notice failed for %s: %s", user_id, exc)
