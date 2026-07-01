import asyncio
import html
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import quote

import qrcode
from pyrogram import StopPropagation, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import Bot
from config import (
    ADMINS,
    PAYMENT_EXPIRY_MINUTES,
    PAYMENT_REVIEW_CHAT,
    PREMIUM_PLANS,
    UPI_ID,
    UPI_NAME,
)
from database.database import (
    add_payment_log,
    check_premium_access,
    claim_payment_submission,
    clear_pending_plan,
    get_pending_plan,
    give_premium,
    remove_premium,
    set_pending_plan,
)


WAITING_SCREENSHOT = set()


async def delete_later(message, delay=300):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


async def delete_page(message, include_command=False):
    try:
        await message.delete()
    except Exception:
        pass
    if include_command and getattr(message, "reply_to_message", None):
        try:
            await message.reply_to_message.delete()
        except Exception:
            pass


async def expire_payment_qr(client, user_id, qr_message):
    await asyncio.sleep(60)
    try:
        await qr_message.delete()
    except Exception:
        pass
    plan = await get_pending_plan(user_id)
    if not plan or plan.get("status") == "submitted":
        return
    prompt = await client.send_message(
        user_id,
        "📸 <b>Submit your payment screenshot</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🟢 Submit Your Screenshot", callback_data="pay:paid")]]
        ),
    )
    asyncio.create_task(delete_later(prompt))


@Bot.on_message(filters.command("mypremium") & filters.private, group=-90)
async def my_premium(client, message):
    expiry = await check_premium_access(message.from_user.id)
    if not expiry:
        await message.reply_text(
            "🆓 Premium is not active.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✨ Get Premium", callback_data="buy_access")]]
            ),
        )
        return
    await message.reply_text(
        "💎 <b>Premium Active</b>\n\n"
        f"Expires: <code>{expiry.strftime('%d %b %Y, %I:%M %p')}</code>"
    )


@Bot.on_message(filters.command("addpremium") & filters.private & filters.user(ADMINS), group=-90)
async def add_premium_command(client, message):
    try:
        if len(message.command) != 3:
            raise ValueError("Usage: /addpremium USER_ID DAYS")
        user_id, days = map(int, message.command[1:])
        expiry = await give_premium(user_id, days)
        user = await client.get_users(user_id)
        await add_payment_log(
            user_id,
            getattr(user, "username", None),
            days,
            0,
            "Approved",
            message.from_user.id,
        )
        await message.reply_text(
            f"✅ Premium extended by <code>{days}</code> day(s).\n"
            f"New expiry: <code>{expiry.strftime('%d %b %Y, %I:%M %p')}</code>"
        )
        await client.send_message(user_id, f"✅ Premium active until <code>{expiry.strftime('%d %b %Y, %I:%M %p')}</code>.")
    except Exception as exc:
        await message.reply_text(f"❌ Add premium failed: <code>{exc}</code>")
    raise StopPropagation


@Bot.on_message(filters.command("removepremium") & filters.private & filters.user(ADMINS), group=-90)
async def remove_premium_command(client, message):
    try:
        if len(message.command) != 2:
            raise ValueError("Usage: /removepremium USER_ID")
        user_id = int(message.command[1])
        changed = await remove_premium(user_id)
        await message.reply_text(
            "✅ Premium successfully removed." if changed else "ℹ️ User had no active premium."
        )
        if changed:
            await client.send_message(user_id, "❌ Your premium access was removed by an administrator.")
    except Exception as exc:
        await message.reply_text(f"❌ Remove premium failed: <code>{exc}</code>")
    raise StopPropagation


def plans_markup():
    rows, row = [], []
    for days, price in sorted(PREMIUM_PLANS.items()):
        row.append(
            InlineKeyboardButton(
                f"💎 {days} Days • ₹{price}",
                callback_data=f"pay:plan:{days}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="pay:close")])
    return InlineKeyboardMarkup(rows)


def upi_url(days, price):
    note = quote(f"Premium access {days} days")
    return (
        f"upi://pay?pa={quote(UPI_ID)}&pn={quote(UPI_NAME)}"
        f"&am={int(price)}&cu=INR&tn={note}"
    )


async def send_plan_menu(message, user_id):
    if not PREMIUM_PLANS or not UPI_ID:
        sent = await message.reply_text(
            "⚠️ Premium payments are not configured yet. Please contact the administrator."
        )
        asyncio.create_task(delete_later(sent))
        return sent
    current = await check_premium_access(user_id)
    status = current.strftime("%d %b %Y, %I:%M %p") if current else "Not active"
    return await message.reply_text(
        "<b>💎 Premium Access</b>\n\n"
        f"Current expiry: <code>{status}</code>\n\n"
        "Choose a plan. Existing active time is automatically extended.",
        reply_markup=plans_markup(),
    )


@Bot.on_callback_query(filters.regex(r"^buy_access$"), group=-90)
async def buy_access(client, query):
    await query.answer("Choose a premium plan")
    await send_plan_menu(query.message, query.from_user.id)
    await delete_page(query.message, include_command=True)


@Bot.on_callback_query(filters.regex(r"^pay:"), group=-90)
async def payment_callbacks(client, query):
    parts = query.data.split(":")
    action = parts[1]
    user_id = query.from_user.id
    try:
        if action == "close":
            await query.message.delete()
            return
        if action == "plan":
            days = int(parts[2])
            price = PREMIUM_PLANS.get(days)
            if not price or not UPI_ID:
                raise RuntimeError("This premium plan is not configured")
            await set_pending_plan(user_id, days, price)
            image = qrcode.make(upi_url(days, price))
            buffer = BytesIO()
            buffer.name = "premium-payment.png"
            image.save(buffer, format="PNG")
            buffer.seek(0)
            await query.answer("Payment QR generated")
            sent = await query.message.reply_photo(
                buffer,
                caption=(
                    f"<b>💳 Premium Payment</b>\n\n"
                    f"Plan: <code>{days} days</code>\n"
                    f"Amount: <code>₹{price}</code>\n"
                    "Pay the exact amount. This QR will close automatically in 1 minute."
                ),
            )
            asyncio.create_task(expire_payment_qr(client, user_id, sent))
            await delete_page(query.message)
            return
        if action == "paid":
            plan = await get_pending_plan(user_id)
            if not plan:
                raise RuntimeError("No pending payment plan found")
            if plan.get("status") == "submitted":
                await query.answer("Screenshot already submitted", show_alert=True)
                return
            created = plan.get("created_at")
            if created and datetime.utcnow() - created > timedelta(minutes=PAYMENT_EXPIRY_MINUTES):
                await clear_pending_plan(user_id)
                raise RuntimeError("Payment session expired; select the plan again")
            WAITING_SCREENSHOT.add(user_id)
            await query.answer("Send payment screenshot", show_alert=True)
            prompt = await query.message.reply_text(
                "📸 Send your payment screenshot now. Premium activates only after admin approval."
            )
            asyncio.create_task(delete_later(prompt))
            await delete_page(query.message)
            return
        if action in ("approve", "reject"):
            if user_id not in ADMINS:
                await query.answer("Admins only", show_alert=True)
                return
            customer_id = int(parts[2])
            plan = await get_pending_plan(customer_id)
            if not plan:
                raise RuntimeError("Payment request is no longer pending")
            days, price = int(plan["days"]), int(plan["price"])
            customer = await client.get_users(customer_id)
            username = getattr(customer, "username", None)
            if action == "approve":
                expiry = await give_premium(customer_id, days)
                await add_payment_log(customer_id, username, days, price, "Approved", user_id)
                await clear_pending_plan(customer_id)
                await client.send_message(
                    customer_id,
                    "✅ <b>Payment approved</b>\n\n"
                    f"Premium active until <code>{expiry.strftime('%d %b %Y, %I:%M %p')}</code>.\n"
                    "Open your original file link again.",
                )
                await query.answer("Premium activated", show_alert=True)
                await query.message.edit_caption((query.message.caption or "") + "\n\n✅ APPROVED")
            else:
                await add_payment_log(customer_id, username, days, price, "Rejected", user_id)
                await clear_pending_plan(customer_id)
                await client.send_message(customer_id, "❌ Payment rejected. Contact support if this is incorrect.")
                await query.answer("Payment rejected", show_alert=True)
                await query.message.edit_caption((query.message.caption or "") + "\n\n❌ REJECTED")
            return
    except Exception as exc:
        client.LOGGER(__name__).exception("Payment action failed: %s", query.data)
        await query.answer("Payment action failed", show_alert=True)
        await query.message.reply_text(f"❌ Payment action failed: <code>{exc}</code>")


@Bot.on_message(
    filters.private & (filters.photo | filters.document),
    group=-70,
)
async def payment_screenshot(client, message):
    user_id = message.from_user.id
    if user_id not in WAITING_SCREENSHOT:
        plan = await get_pending_plan(user_id)
        if plan and plan.get("status") == "submitted":
            await message.delete()
            notice = await client.send_message(user_id, "ℹ️ Screenshot already submitted. Please wait for review.")
            asyncio.create_task(delete_later(notice))
            raise StopPropagation
        return
    WAITING_SCREENSHOT.discard(user_id)
    plan = await get_pending_plan(user_id)
    if not plan:
        await message.reply_text("❌ Payment session expired. Choose a plan again.")
        raise StopPropagation
    if message.document and not str(message.document.mime_type or "").startswith("image/"):
        await message.reply_text("Send an image screenshot only.")
        raise StopPropagation
    if not await claim_payment_submission(user_id):
        await message.delete()
        notice = await client.send_message(user_id, "ℹ️ Screenshot already submitted. Please wait for review.")
        asyncio.create_task(delete_later(notice))
        raise StopPropagation
    if message.from_user.username:
        identity = f'<a href="https://t.me/{html.escape(message.from_user.username)}">@{html.escape(message.from_user.username)}</a>'
    else:
        display = html.escape(message.from_user.first_name or str(user_id))
        identity = f'<a href="tg://user?id={user_id}">{display}</a>'
    caption = (
        "<b>🧾 Payment Review</b>\n\n"
        f"User: <code>{user_id}</code>\n"
        f"Profile: {identity}\n"
        f"Plan: <code>{plan['days']} days</code>\n"
        f"Amount: <code>₹{plan['price']}</code>"
    )
    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve", callback_data=f"pay:approve:{user_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"pay:reject:{user_id}"),
        ]]
    )
    if message.photo:
        await client.send_photo(PAYMENT_REVIEW_CHAT, message.photo.file_id, caption=caption, reply_markup=markup)
    else:
        await client.send_document(PAYMENT_REVIEW_CHAT, message.document.file_id, caption=caption, reply_markup=markup)
    await message.delete()
    notice = await client.send_message(user_id, "✅ Screenshot received. Please wait while the administrator reviews it.")
    asyncio.create_task(delete_later(notice))
    raise StopPropagation
