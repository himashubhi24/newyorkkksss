from datetime import datetime, timezone

from database.database import database


settings = database["premium_settings"]
repost_pairs = database["premium_repost_pairs"]


def _now():
    return datetime.now(timezone.utc)


async def get_setting(key, default=None):
    item = await settings.find_one({"_id": key})
    return item.get("value", default) if item else default


async def set_setting(key, value, updated_by=None):
    result = await settings.update_one(
        {"_id": key},
        {
            "$set": {
                "value": value,
                "updated_at": _now(),
                "updated_by": updated_by,
            }
        },
        upsert=True,
    )
    return result.acknowledged


async def delete_setting(key):
    result = await settings.delete_one({"_id": key})
    return result.deleted_count


async def get_userbot_session():
    value = await get_setting("userbot_session", "")
    return str(value or "")


async def save_userbot_session(value, updated_by=None):
    return await set_setting("userbot_session", value, updated_by)


async def remove_userbot_session():
    await set_setting("auto_repost_enabled", False)
    return await delete_setting("userbot_session")


async def is_auto_repost_enabled(default=False):
    return bool(await get_setting("auto_repost_enabled", default))


async def set_auto_repost_enabled(enabled, updated_by=None):
    return await set_setting("auto_repost_enabled", bool(enabled), updated_by)


async def add_repost_pair(source, target, updated_by=None):
    result = await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        {
            "$set": {
                "active": True,
                "updated_at": _now(),
                "updated_by": updated_by,
            },
            "$setOnInsert": {"created_at": _now(), "processed": 0},
        },
        upsert=True,
    )
    return result.acknowledged


async def remove_repost_pair(source, target=None):
    query = {"source": int(source)}
    if target is not None:
        query["target"] = int(target)
    result = await repost_pairs.delete_many(query)
    return result.deleted_count


async def list_repost_pairs(active_only=False):
    query = {"active": True} if active_only else {}
    return [item async for item in repost_pairs.find(query).sort("created_at", 1)]


async def mark_repost_processed(source, target):
    await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        {
            "$inc": {"processed": 1},
            "$set": {"last_post_at": _now(), "last_error": None},
        },
    )


async def mark_repost_error(source, target, error):
    await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        {"$set": {"last_error": str(error)[:500], "last_error_at": _now()}},
    )


async def get_force_sub_channels():
    value = await get_setting("force_sub_channels", [])
    return value if isinstance(value, list) else []


async def add_force_sub_channel(channel):
    channels = await get_force_sub_channels()
    channels = [item for item in channels if int(item["id"]) != int(channel["id"])]
    channels.append(channel)
    await set_setting("force_sub_channels", channels, channel.get("added_by"))
    return channel


async def remove_force_sub_channel(channel_id):
    channels = await get_force_sub_channels()
    remaining = [item for item in channels if int(item["id"]) != int(channel_id)]
    if len(remaining) == len(channels):
        return 0
    await set_setting("force_sub_channels", remaining)
    return 1
