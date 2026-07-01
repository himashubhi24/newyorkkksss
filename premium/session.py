import base64
import struct

from pyrogram import Client
from telethon import TelegramClient
from telethon.sessions import MemorySession

from config import API_HASH, APP_ID


SESSION_FORMAT = ">BI?256sQ?"


async def validate_pyrogram_session(session_string):
    client = Client(
        "premium_session_probe",
        api_id=APP_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.start()
        me = await client.get_me()
        if me.is_bot:
            raise RuntimeError("A Telegram user account session is required")
        return me
    finally:
        if client.is_connected:
            await client.stop()


async def session_status(session_string):
    if not session_string:
        return {"active": False, "connected": False, "user": None, "error": None}
    try:
        user = await validate_pyrogram_session(session_string)
        return {"active": True, "connected": True, "user": user, "error": None}
    except Exception as exc:
        return {
            "active": True,
            "connected": False,
            "user": None,
            "error": str(exc),
        }


def telethon_session_to_pyrogram(client, user):
    auth_key = getattr(getattr(client.session, "auth_key", None), "key", None)
    dc_id = getattr(client.session, "dc_id", None)
    if not auth_key or not dc_id or not user:
        raise RuntimeError("QR login did not produce a complete Telegram session")
    packed = struct.pack(
        SESSION_FORMAT,
        int(dc_id),
        int(APP_ID),
        False,
        auth_key,
        int(user.id),
        False,
    )
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


async def create_qr_client():
    client = TelegramClient(MemorySession(), APP_ID, API_HASH)
    await client.connect()
    return client
