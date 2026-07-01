import asyncio
from io import BytesIO

import qrcode
from pyrogram import Client, StopPropagation, filters
from pyrogram.errors import (
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon.errors import SessionPasswordNeededError

from bot import Bot
from config import ADMINS, API_HASH, APP_ID
from premium.session import (
    create_qr_client,
    session_status,
    telethon_session_to_pyrogram,
    validate_pyrogram_session,
)
from premium.storage import (
    add_force_sub_channel,
    add_repost_pair,
    get_force_sub_channels,
    get_userbot_session,
    is_auto_repost_enabled,
    list_repost_pairs,
    remove_force_sub_channel,
    remove_repost_pair,
    remove_userbot_session,
    save_userbot_session,
    set_auto_repost_enabled,
    set_repost_interval,
)


PENDING = {}
LOGIN_CLIENTS = {}
QR_CLIENTS = {}


def panel_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔵 Phone Login", callback_data="premium:phone"),
                InlineKeyboardButton("🟣 QR Login", callback_data="premium:qr"),
            ],
            [
                InlineKeyboardButton("🔑 Add Session", callback_data="premium:session"),
                InlineKeyboardButton("📊 Userbot Status", callback_data="premium:status"),
            ],
            [
                InlineKeyboardButton("📥 Add Source/Target", callback_data="premium:add_pair"),
                InlineKeyboardButton("🗑 Remove Pair", callback_data="premium:remove_pair"),
            ],
            [InlineKeyboardButton("⏱ Set Repost Interval", callback_data="premium:set_interval")],
            [
                InlineKeyboardButton("🟢 Enable Repost", callback_data="premium:enable"),
                InlineKeyboardButton("🔴 Disable Repost", callback_data="premium:disable"),
            ],
            [
                InlineKeyboardButton("🔵 Normal Join", callback_data="premium:add_fsub"),
                InlineKeyboardButton("🟣 Request Join", callback_data="premium:add_request_fsub"),
            ],
            [
                InlineKeyboardButton("➖ Remove Force Sub", callback_data="premium:remove_fsub"),
            ],
            [
                InlineKeyboardButton("📋 Repost Status", callback_data="premium:repost_status"),
                InlineKeyboardButton("🚪 Remove Userbot", callback_data="premium:remove_session"),
            ],
            [InlineKeyboardButton("✖️ Close", callback_data="premium:close")],
        ]
    )


async def restart_worker(bot):
    worker = getattr(bot, "auto_repost_worker", None)
    if not worker:
        raise RuntimeError("Auto repost worker is not initialized")
    return await worker.restart()


async def save_and_start(bot, session, admin_id):
    user = await validate_pyrogram_session(session)
    await save_userbot_session(session, admin_id)
    await set_auto_repost_enabled(True, admin_id)
    started = await restart_worker(bot)
    return user, started


def user_label(user):
    if not user:
        return "-"
    name = getattr(user, "first_name", None) or "-"
    username = getattr(user, "username", None)
    return f"{name} ({'@' + username if username else 'no username'})"


async def show_panel(message, heading="<b>💎 Premium Control Center</b>"):
    await message.reply_text(
        heading + "\n\nManage sessions, reposting, and access controls.",
        reply_markup=panel_markup(),
    )


@Bot.on_message(filters.command(["premiumadmin", "admin"]) & filters.private & filters.user(ADMINS), group=-90)
async def premium_admin(client, message):
    PENDING.pop(message.from_user.id, None)
    await show_panel(message)
    raise StopPropagation


async def finish_qr_login(bot, admin_id, qr_client, qr_login, qr_message):
    try:
        user = await qr_login.wait()
        session = telethon_session_to_pyrogram(qr_client, user)
        pyrogram_user, started = await save_and_start(bot, session, admin_id)
        await bot.send_message(
            admin_id,
            "✅ <b>QR login successful</b>\n"
            f"Account: <code>{user_label(pyrogram_user)}</code>\n"
            f"Auto repost: <code>{'running' if started else 'waiting for source/target'}</code>",
        )
    except SessionPasswordNeededError:
        PENDING[admin_id] = "qr_password"
        QR_CLIENTS[admin_id] = qr_client
        await bot.send_message(admin_id, "🔐 QR accepted. Send your Telegram 2FA password.")
        return
    except asyncio.TimeoutError:
        await bot.send_message(admin_id, "⏳ QR expired. Open Premium Control Center and generate a new QR.")
    except Exception as exc:
        bot.LOGGER(__name__).exception("QR login failed")
        await bot.send_message(admin_id, f"❌ QR login failed: <code>{exc}</code>")
    finally:
        try:
            await qr_message.delete()
        except Exception:
            pass
        if PENDING.get(admin_id) != "qr_password":
            QR_CLIENTS.pop(admin_id, None)
            await qr_client.disconnect()


@Bot.on_callback_query(filters.regex(r"^premium:") & filters.user(ADMINS), group=-90)
async def premium_callbacks(client, query):
    action = query.data.split(":", 1)[1]
    admin_id = query.from_user.id
    try:
        if action == "close":
            await query.message.delete()
            return
        if action == "phone":
            PENDING[admin_id] = "phone"
            await query.answer("Send phone number")
            await query.message.reply_text("📱 Send phone number with country code, e.g. <code>+91XXXXXXXXXX</code>.")
            return
        if action == "session":
            PENDING[admin_id] = "session"
            await query.answer("Send session string")
            await query.message.reply_text("🔑 Send a valid Pyrogram user session string.")
            return
        if action == "qr":
            await query.answer("Generating secure QR")
            qr_client = await create_qr_client()
            qr_login = await qr_client.qr_login()
            image = qrcode.make(qr_login.url)
            buffer = BytesIO()
            buffer.name = "telegram-login.png"
            image.save(buffer, format="PNG")
            buffer.seek(0)
            sent = await query.message.reply_photo(
                buffer,
                caption=(
                    "<b>📷 Telegram QR Login</b>\n\n"
                    "Telegram → Settings → Devices → Link Desktop Device.\n"
                    "Scan this QR. It expires shortly. Never share it."
                ),
            )
            QR_CLIENTS[admin_id] = qr_client
            asyncio.create_task(finish_qr_login(client, admin_id, qr_client, qr_login, sent))
            return
        if action == "status":
            worker = getattr(client, "auto_repost_worker", None)
            connected = bool(worker and worker.connected)
            if connected:
                user = worker.client.me or await worker.client.get_me()
                state = {"active": True, "user": user, "error": None}
            else:
                state = await session_status(await get_userbot_session())
                user = state["user"]
            phone = getattr(user, "phone_number", None) or "-"
            await query.answer("Status refreshed")
            await query.message.reply_text(
                "<b>📊 Userbot Status</b>\n\n"
                f"Session: <code>{'active' if state['active'] else 'inactive'}</code>\n"
                f"Connection: <code>{'connected' if connected else 'disconnected'}</code>\n"
                f"Phone: <code>{phone}</code>\n"
                f"Account: <code>{user_label(user)}</code>\n"
                f"Validation: <code>{state['error'] or 'OK'}</code>"
            )
            return
        if action == "add_pair":
            PENDING[admin_id] = "pair"
            await query.answer("Send source and target")
            await query.message.reply_text("Send: <code>-100SOURCE -100TARGET</code>")
            return
        if action == "remove_pair":
            PENDING[admin_id] = "remove_pair"
            await query.answer("Send pair to remove")
            await query.message.reply_text("Send <code>-100SOURCE -100TARGET</code>, or only source to remove all its targets.")
            return
        if action == "set_interval":
            PENDING[admin_id] = "set_interval"
            await query.answer("Set repost interval")
            await query.message.reply_text(
                "⏱ Send: <code>-100SOURCE -100TARGET HOURS</code>\n"
                "Use <code>0</code> for instant repost or <code>1-24</code> hours."
            )
            return
        if action == "enable":
            await set_auto_repost_enabled(True, admin_id)
            started = await restart_worker(client)
            await query.answer("Auto repost enabled", show_alert=True)
            await query.message.reply_text(
                "✅ Auto repost enabled and connected."
                if started else "✅ Auto repost enabled; waiting for a valid session and source/target pair."
            )
            return
        if action == "disable":
            await set_auto_repost_enabled(False, admin_id)
            worker = getattr(client, "auto_repost_worker", None)
            if worker:
                await worker.stop()
            await query.answer("Auto repost disabled", show_alert=True)
            await query.message.reply_text("✅ Auto repost successfully disabled.")
            return
        if action == "remove_session":
            worker = getattr(client, "auto_repost_worker", None)
            if worker:
                await worker.stop()
            removed = await remove_userbot_session()
            await query.answer("Session removed", show_alert=True)
            await query.message.reply_text(
                "✅ Userbot session removed and auto repost disabled."
                if removed else "ℹ️ No saved userbot session was found."
            )
            return
        if action == "add_fsub":
            PENDING[admin_id] = "add_fsub"
            await query.answer("Send force-sub channel ID")
            await query.message.reply_text("Send a numeric channel ID. Bot must be a member/admin there.")
            return
        if action == "add_request_fsub":
            PENDING[admin_id] = "add_request_fsub"
            await query.answer("Send request-to-join channel ID")
            await query.message.reply_text(
                "📝 Send a numeric request-to-join channel ID. Bot must be admin with invite permission."
            )
            return
        if action == "remove_fsub":
            PENDING[admin_id] = "remove_fsub"
            await query.answer("Send channel ID")
            channels = await get_force_sub_channels()
            current = "\n".join(f"<code>{x['id']}</code> • {x.get('title', 'Channel')}" for x in channels) or "None"
            await query.message.reply_text(f"Current dynamic force-subs:\n{current}\n\nSend the ID to remove.")
            return
        if action == "repost_status":
            enabled = await is_auto_repost_enabled(False)
            pairs = await list_repost_pairs()
            worker = getattr(client, "auto_repost_worker", None)
            lines = [
                "<b>📡 Auto Repost Status</b>",
                f"Enabled: <code>{enabled}</code>",
                f"Connected: <code>{bool(worker and worker.connected)}</code>",
                f"Pairs: <code>{len(pairs)}</code>",
            ]
            for pair in pairs[:20]:
                lines.append(
                    f"<code>{pair['source']}</code> → <code>{pair['target']}</code> "
                    f"• interval <code>{pair.get('interval_hours', 0)}h</code> "
                    f"• queued <code>{len(pair.get('pending_message_ids') or [])}</code> "
                    f"• processed <code>{pair.get('processed', 0)}</code> "
                    f"• error <code>{pair.get('last_error') or '-'}</code>"
                )
            await query.answer("Status refreshed")
            await query.message.reply_text("\n".join(lines))
            return
    except Exception as exc:
        client.LOGGER(__name__).exception("Premium admin callback failed: %s", action)
        await query.answer("Action failed", show_alert=True)
        await query.message.reply_text(f"❌ Action failed: <code>{exc}</code>")


@Bot.on_message(
    filters.private & filters.user(ADMINS) & filters.text & ~filters.regex(r"^/"),
    group=-80,
)
async def premium_pending_input(client, message):
    admin_id = message.from_user.id
    action = PENDING.get(admin_id)
    if not action:
        return
    text = (message.text or "").strip()
    try:
        if action == "session":
            if len(text) < 100:
                raise ValueError("Session string is too short")
            user, started = await save_and_start(client, text, admin_id)
            PENDING.pop(admin_id, None)
            await message.reply_text(
                "✅ Session successfully added.\n"
                f"Account: <code>{user_label(user)}</code>\n"
                f"Auto repost: <code>{'running' if started else 'waiting for source/target'}</code>"
            )
        elif action == "phone":
            phone = text.replace(" ", "")
            login = Client(f"premium_login_{admin_id}", api_id=APP_ID, api_hash=API_HASH, in_memory=True)
            await login.connect()
            try:
                sent = await login.send_code(phone)
            except PhoneNumberInvalid:
                await login.disconnect()
                raise ValueError("Invalid phone number")
            LOGIN_CLIENTS[admin_id] = {"client": login, "phone": phone, "hash": sent.phone_code_hash}
            PENDING[admin_id] = "otp"
            await message.reply_text("📨 OTP sent in Telegram. Send the digits here.")
        elif action == "otp":
            state = LOGIN_CLIENTS.get(admin_id)
            if not state:
                raise RuntimeError("Login expired; start Phone Login again")
            try:
                await state["client"].sign_in(state["phone"], state["hash"], text.replace(" ", ""))
            except SessionPasswordNeeded:
                PENDING[admin_id] = "password"
                await message.reply_text("🔐 Send your Telegram 2FA password.")
                raise StopPropagation
            except (PhoneCodeInvalid, PhoneCodeExpired):
                raise ValueError("OTP is invalid or expired")
            session = await state["client"].export_session_string()
            await state["client"].disconnect()
            LOGIN_CLIENTS.pop(admin_id, None)
            user, started = await save_and_start(client, session, admin_id)
            PENDING.pop(admin_id, None)
            await message.reply_text(f"✅ Login successful: <code>{user_label(user)}</code>. Repost running: <code>{started}</code>")
        elif action == "password":
            state = LOGIN_CLIENTS.get(admin_id)
            if not state:
                raise RuntimeError("Login expired; start Phone Login again")
            try:
                await state["client"].check_password(text)
            except PasswordHashInvalid:
                raise ValueError("Incorrect 2FA password")
            session = await state["client"].export_session_string()
            await state["client"].disconnect()
            LOGIN_CLIENTS.pop(admin_id, None)
            user, started = await save_and_start(client, session, admin_id)
            PENDING.pop(admin_id, None)
            await message.reply_text(f"✅ Login successful: <code>{user_label(user)}</code>. Repost running: <code>{started}</code>")
        elif action == "qr_password":
            qr_client = QR_CLIENTS.get(admin_id)
            if not qr_client:
                raise RuntimeError("QR login expired; generate a new QR")
            user = await qr_client.sign_in(password=text)
            session = telethon_session_to_pyrogram(qr_client, user)
            await qr_client.disconnect()
            QR_CLIENTS.pop(admin_id, None)
            pyrogram_user, started = await save_and_start(client, session, admin_id)
            PENDING.pop(admin_id, None)
            await message.reply_text(f"✅ QR login successful: <code>{user_label(pyrogram_user)}</code>. Repost running: <code>{started}</code>")
        elif action == "pair":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError("Send exactly: -100SOURCE -100TARGET")
            source, target = map(int, parts)
            worker = getattr(client, "auto_repost_worker", None)
            if worker and worker.connected:
                await worker.client.get_chat(source)
                await worker.client.get_chat(target)
            if not await add_repost_pair(source, target, admin_id):
                raise RuntimeError("Database did not acknowledge the pair")
            PENDING.pop(admin_id, None)
            started = await restart_worker(client)
            await message.reply_text(
                f"✅ Source <code>{source}</code> and target <code>{target}</code> successfully added.\n"
                f"Worker running: <code>{started}</code>"
            )
        elif action == "remove_pair":
            parts = text.split()
            if len(parts) not in (1, 2):
                raise ValueError("Send source, optionally followed by target")
            source = int(parts[0])
            target = int(parts[1]) if len(parts) == 2 else None
            removed = await remove_repost_pair(source, target)
            if not removed:
                raise RuntimeError("No matching source/target pair found")
            PENDING.pop(admin_id, None)
            await restart_worker(client)
            await message.reply_text(f"✅ Successfully removed <code>{removed}</code> repost pair(s).")
        elif action == "set_interval":
            parts = text.split()
            if len(parts) != 3:
                raise ValueError("Send exactly: -100SOURCE -100TARGET HOURS")
            source, target, hours = map(int, parts)
            if hours < 0 or hours > 24:
                raise ValueError("Hours must be between 0 and 24")
            if not await set_repost_interval(source, target, hours, admin_id):
                raise RuntimeError("No matching source/target pair found")
            PENDING.pop(admin_id, None)
            await restart_worker(client)
            mode = "instant" if hours == 0 else f"every {hours} hour(s)"
            await message.reply_text(f"✅ Repost interval successfully set to <code>{mode}</code>.")
        elif action in ("add_fsub", "add_request_fsub"):
            channel_id = int(text)
            chat = await client.get_chat(channel_id)
            me = await client.get_me()
            await client.get_chat_member(channel_id, me.id)
            request_mode = action == "add_request_fsub"
            if request_mode:
                invite = await client.create_chat_invite_link(
                    channel_id,
                    creates_join_request=True,
                    name="Premium Request FSub",
                )
                link = invite.invite_link
            else:
                link = getattr(chat, "invite_link", None)
                if not link:
                    link = await client.export_chat_invite_link(channel_id)
            item = {
                "id": channel_id,
                "title": chat.title or "Channel",
                "link": link,
                "added_by": admin_id,
                "request_mode": request_mode,
            }
            await add_force_sub_channel(item)
            PENDING.pop(admin_id, None)
            mode = "request-to-join" if request_mode else "normal"
            await message.reply_text(
                f"✅ <b>{item['title']}</b> successfully added as {mode} force subscribe."
            )
        elif action == "remove_fsub":
            removed = await remove_force_sub_channel(int(text))
            if not removed:
                raise RuntimeError("Channel is not configured in dynamic force subscribe")
            PENDING.pop(admin_id, None)
            await message.reply_text("✅ Force-sub channel successfully removed.")
    except StopPropagation:
        raise
    except Exception as exc:
        client.LOGGER(__name__).exception("Premium admin input failed: %s", action)
        PENDING.pop(admin_id, None)
        await message.reply_text(f"❌ {action.replace('_', ' ').title()} failed: <code>{exc}</code>")
    raise StopPropagation
