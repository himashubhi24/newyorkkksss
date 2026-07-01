import asyncio
import html
from datetime import datetime, timedelta, timezone

from config import PREMIUM_REPORT_CHAT
from database.database import (
    get_approved_payments_between,
    mark_premium_reminder_sent,
    premium_expiry_candidates,
    remove_premium,
)
from premium.storage import get_setting, set_setting


IST = timezone(timedelta(hours=5, minutes=30))


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
        await self.send_daily_report()

    async def send_daily_report(self):
        now_ist = datetime.now(IST)
        report_day = (now_ist - timedelta(days=1)).date()
        report_key = report_day.isoformat()
        if await get_setting("premium_daily_report_date", "") == report_key:
            return
        start_ist = datetime.combine(report_day, datetime.min.time()).replace(tzinfo=IST)
        end_ist = start_ist + timedelta(days=1)
        start_utc = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = end_ist.astimezone(timezone.utc).replace(tzinfo=None)
        payments = await get_approved_payments_between(start_utc, end_utc)
        total = sum(int(item.get("amount", 0)) for item in payments)
        lines = [
            "<b>📊 Daily Premium Activation Report</b>",
            f"Date: <code>{report_key}</code>",
            f"Activations: <code>{len(payments)}</code>",
            f"Revenue: <code>₹{total}</code>",
            "",
        ]
        for item in payments[:80]:
            user_id = int(item.get("user_id", 0))
            username = item.get("username")
            if username:
                profile = f'<a href="https://t.me/{html.escape(username)}">@{html.escape(username)}</a>'
            else:
                profile = f'<a href="tg://user?id={user_id}">User {user_id}</a>'
            lines.append(
                f"• {profile} • <code>{item.get('plan_days')}d</code> • <code>₹{item.get('amount')}</code>"
            )
        if len(payments) > 80:
            lines.append(f"\n…and {len(payments) - 80} more activations")
        await self.bot.send_message(PREMIUM_REPORT_CHAT, "\n".join(lines), disable_web_page_preview=True)
        await set_setting("premium_daily_report_date", report_key)
