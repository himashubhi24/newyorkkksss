from pyrogram import StopPropagation, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import Bot
from premium.storage import get_force_sub_channels


JOINED = {
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}


async def missing_channels(client, user_id):
    missing = []
    for item in await get_force_sub_channels():
        try:
            member = await client.get_chat_member(int(item["id"]), user_id)
            if member.status not in JOINED:
                missing.append(item)
        except UserNotParticipant:
            missing.append(item)
        except Exception as exc:
            client.LOGGER(__name__).warning("Force-sub check failed for %s: %s", item.get("id"), exc)
            missing.append(item)
    return missing


@Bot.on_message(filters.command("start") & filters.private, group=-200)
async def premium_force_sub_gate(client, message):
    missing = await missing_channels(client, message.from_user.id)
    if not missing:
        return
    rows = []
    for item in missing:
        label = item.get("title") or "Join Channel"
        rows.append([InlineKeyboardButton(f"📢 {label}", url=item["link"])])
    payload = message.command[1] if len(message.command) > 1 else ""
    retry = f"https://t.me/{client.username}"
    if payload:
        retry += f"?start={payload}"
    rows.append([InlineKeyboardButton("✅ I Joined • Try Again", url=retry)])
    await message.reply_text(
        "<b>🔐 Premium Access Gate</b>\n\nJoin the required channel(s), then tap Try Again.",
        reply_markup=InlineKeyboardMarkup(rows),
        disable_web_page_preview=True,
    )
    raise StopPropagation
