import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from pyrogram import raw
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.database import database
from helper_func import encode


DEEPLINK_RE = re.compile(
    r"(?P<link>(?:https?://)?(?:t\.me|telegram\.me)/(?P<bot>[A-Za-z0-9_]+)\?start=(?P<param>[A-Za-z0-9_\-=]+)|@(?P<atbot>[A-Za-z0-9_]+)\?start=(?P<atparam>[A-Za-z0-9_\-=]+))",
    re.IGNORECASE,
)
GATE_WORDS = ("join", "subscribe", "channel", "required", "request", "must", "verify", "check", "first")
mapping_col = database["premium_deeplink_mappings"]
conversion_lock = asyncio.Lock()


def message_text(message):
    text = getattr(message, "caption", None) or getattr(message, "text", None) or ""
    return text.html if hasattr(text, "html") else str(text)


def extract_deeplinks(message):
    values = [message_text(message)]
    markup = getattr(message, "reply_markup", None)
    if markup:
        for row in markup.inline_keyboard or []:
            values.extend(button.url for button in row if getattr(button, "url", None))
    found = []
    seen = set()
    for value in values:
        for match in DEEPLINK_RE.finditer(value or ""):
            bot = match.group("bot") or match.group("atbot")
            param = match.group("param") or match.group("atparam")
            full = match.group("link")
            key = f"https://t.me/{bot}?start={param}"
            if key.lower() not in seen:
                seen.add(key.lower())
                found.append({"full_link": full, "key": key, "bot": bot, "param": param})
    return found


def media_type(message):
    for name in ("photo", "video", "document", "audio", "animation"):
        if getattr(message, name, None):
            return name
    return None


def delivered_file(message):
    kind = media_type(message)
    if not kind:
        return False
    if kind == "video":
        return True
    text = message_text(message).lower()
    return not any(word in text for word in GATE_WORDS)


def invite_target(url):
    if not url:
        return None
    parsed = urlparse(url if url.startswith("http") else "https://" + url)
    if parsed.netloc not in ("t.me", "telegram.me"):
        return None
    path = parsed.path.strip("/")
    if not path or "start=" in (parsed.query or "").lower():
        return None
    if path.startswith("+") or path.startswith("joinchat/"):
        return url if url.startswith("http") else "https://" + url
    name = path.split("/")[0]
    return None if name in ("c", "s") or name.lower().endswith("bot") else name


async def join_target(userbot, target, logger):
    try:
        parsed = urlparse(target) if str(target).startswith("http") else None
        path = parsed.path.strip("/") if parsed else ""
        if path.startswith("+") or path.startswith("joinchat/"):
            invite_hash = path[1:] if path.startswith("+") else path.split("/", 1)[1]
            await userbot.invoke(raw.functions.messages.ImportChatInvite(hash=invite_hash))
        else:
            await userbot.join_chat(target)
        return True
    except FloodWait as exc:
        if exc.value <= 60:
            await asyncio.sleep(exc.value + 1)
            return await join_target(userbot, target, logger)
    except Exception as exc:
        value = str(exc).lower()
        if "already" in value or "request" in value:
            return True
        logger.warning("Conversion force-sub join failed target=%s error=%s", target, exc)
    return False


async def fetch_source_files(userbot, bot_username, param, logger, timeout=180):
    since = time.time()
    await userbot.send_message(bot_username, f"/start {param}")
    retried = False
    seen_targets = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        messages = []
        try:
            async for item in userbot.get_chat_history(bot_username, limit=80):
                if item.date and item.date.timestamp() >= since:
                    messages.append(item)
        except Exception as exc:
            logger.warning("Conversion source history failed bot=%s error=%s", bot_username, exc)
            await asyncio.sleep(2)
            continue
        messages.reverse()
        files = [item for item in messages if delivered_file(item)]
        if files:
            return files
        handled = False
        for item in messages:
            markup = getattr(item, "reply_markup", None)
            if not markup:
                continue
            gate_text = message_text(item).lower()
            is_gate = any(word in gate_text for word in GATE_WORDS)
            for row_index, row in enumerate(markup.inline_keyboard or []):
                for col_index, button in enumerate(row):
                    target = invite_target(getattr(button, "url", None))
                    if target and target not in seen_targets:
                        seen_targets.add(target)
                        handled = await join_target(userbot, target, logger) or handled
                    elif is_gate and not getattr(button, "url", None):
                        try:
                            await item.click(row_index, col_index)
                            handled = True
                        except Exception:
                            pass
        if handled and not retried:
            await asyncio.sleep(2)
            await userbot.send_message(bot_username, f"/start {param}")
            retried = True
        await asyncio.sleep(2)
    return []


async def upload_path(bot, path, source_message):
    caption = source_message.caption.html if source_message.caption else None
    kwargs = {"chat_id": bot.db_channel.id, "caption": caption, "parse_mode": ParseMode.HTML}
    kind = media_type(source_message)
    if kind == "photo":
        return await bot.send_photo(photo=path, **kwargs)
    if kind == "video":
        video = source_message.video
        return await bot.send_video(
            video=path,
            supports_streaming=True,
            duration=getattr(video, "duration", None),
            width=getattr(video, "width", None),
            height=getattr(video, "height", None),
            **kwargs,
        )
    if kind == "audio":
        return await bot.send_audio(audio=path, **kwargs)
    if kind == "animation":
        return await bot.send_animation(animation=path, **kwargs)
    return await bot.send_document(document=path, **kwargs)


async def store_files(bot, userbot, files):
    stored = []
    Path("downloads").mkdir(exist_ok=True)
    for item in files:
        path = None
        try:
            try:
                sent = await item.copy(bot.db_channel.id)
            except Exception:
                path = await userbot.download_media(item, file_name="downloads/")
                if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
                    raise RuntimeError("source media download returned an empty file")
                sent = await upload_path(bot, path, item)
            stored.append(sent.id)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return stored


async def own_deeplink(bot, message_ids):
    if not message_ids:
        return None
    factor = abs(bot.db_channel.id)
    if len(message_ids) == 1:
        payload = await encode(f"get-{message_ids[0] * factor}")
    else:
        payload = await encode(f"get-{message_ids[0] * factor}-{message_ids[-1] * factor}")
    return f"https://t.me/{bot.username}?start={payload}"


async def convert_one(bot, userbot, link, logger):
    cached = await mapping_col.find_one({"_id": link["key"].lower()})
    if cached:
        return cached.get("new_deeplink")
    async with conversion_lock:
        cached = await mapping_col.find_one({"_id": link["key"].lower()})
        if cached:
            return cached.get("new_deeplink")
        files = await fetch_source_files(userbot, link["bot"], link["param"], logger)
        if not files:
            return None
        ids = await store_files(bot, userbot, files)
        converted = await own_deeplink(bot, ids)
        if converted:
            await mapping_col.update_one(
                {"_id": link["key"].lower()},
                {"$set": {"new_deeplink": converted, "source_bot": link["bot"], "file_count": len(ids)}},
                upsert=True,
            )
        return converted


def has_foreign_deeplink(value, own_username):
    for match in DEEPLINK_RE.finditer(value or ""):
        username = match.group("bot") or match.group("atbot")
        if username.lower() != str(own_username).lower():
            return True
    return False


def converted_markup(message, replacements, own_username):
    markup = getattr(message, "reply_markup", None)
    if not markup:
        return None
    rows = []
    for row in markup.inline_keyboard or []:
        new_row = []
        for button in row:
            url = getattr(button, "url", None)
            if not url:
                continue
            for old, new in replacements.items():
                url = url.replace(old, new)
            if has_foreign_deeplink(url, own_username):
                return None
            new_row.append(InlineKeyboardButton(button.text, url=url))
        if new_row:
            rows.append(new_row)
    return InlineKeyboardMarkup(rows) if rows else None


async def convert_post(bot, userbot, message, logger):
    links = extract_deeplinks(message)
    if not links:
        return None
    replacements = {}
    for link in links:
        converted = await convert_one(bot, userbot, link, logger)
        if not converted:
            return None
        replacements[link["full_link"]] = converted
        replacements[link["key"]] = converted
    text = message_text(message)
    for old, new in replacements.items():
        text = text.replace(old, new)
    if has_foreign_deeplink(text, bot.username):
        return None
    markup = converted_markup(message, replacements, bot.username)
    original_markup = getattr(message, "reply_markup", None)
    if original_markup and any(
        has_foreign_deeplink(getattr(button, "url", "") or "", bot.username)
        for row in original_markup.inline_keyboard or []
        for button in row
    ) and markup is None:
        return None
    return {"text": text, "reply_markup": markup, "links": len(links)}


async def publish_converted(userbot, target, message, converted):
    text = converted["text"]
    markup = converted["reply_markup"]
    if media_type(message):
        return await message.copy(target, caption=text or None, reply_markup=markup)
    return await userbot.send_message(
        target,
        text or "Converted post",
        reply_markup=markup,
        disable_web_page_preview=False,
    )
