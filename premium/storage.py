from datetime import datetime, timedelta, timezone

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


async def get_deeplink_admins():
    value = await get_setting("deeplink_admins", [])
    return [int(user_id) for user_id in value] if isinstance(value, list) else []


async def add_deeplink_admin(user_id, updated_by=None):
    admins = await get_deeplink_admins()
    user_id = int(user_id)
    if user_id not in admins:
        admins.append(user_id)
        await set_setting("deeplink_admins", admins, updated_by)
    return user_id


async def remove_deeplink_admin(user_id, updated_by=None):
    admins = await get_deeplink_admins()
    user_id = int(user_id)
    remaining = [item for item in admins if item != user_id]
    if len(remaining) == len(admins):
        return 0
    await set_setting("deeplink_admins", remaining, updated_by)
    return 1


async def is_deeplink_admin(user_id):
    return int(user_id) in await get_deeplink_admins()


async def add_repost_pair(source, target, updated_by=None, interval_hours=0):
    result = await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        {
            "$set": {
                "active": True,
                "updated_at": _now(),
                "updated_by": updated_by,
                "next_post_at": None,
                "interval_hours": int(interval_hours),
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


async def set_repost_interval(source, target, hours, updated_by=None):
    result = await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        {
            "$set": {
                "interval_hours": int(hours),
                "updated_at": _now(),
                "updated_by": updated_by,
                "next_post_at": None,
            }
        },
    )
    return result.matched_count


async def enqueue_repost(source, target, message_id):
    await repost_pairs.update_one(
        {"source": int(source), "target": int(target), "active": True},
        {
            "$addToSet": {"pending_message_ids": int(message_id)},
            "$set": {"updated_at": _now()},
        },
    )


async def configure_repost_from_first(source, target, end_message_id, updated_by=None):
    result = await repost_pairs.update_one(
        {"source": int(source), "target": int(target), "active": True},
        {
            "$set": {
                "backfill_cursor": 1,
                "backfill_end": int(end_message_id),
                "backfill_active": True,
                "next_post_at": None,
                "updated_at": _now(),
                "updated_by": updated_by,
            }
        },
    )
    return result.matched_count


async def get_due_repost_pairs(now):
    query = {
        "active": True,
        "$and": [
            {
                "$or": [
                    {"pending_message_ids.0": {"$exists": True}},
                    {"backfill_active": True},
                ]
            },
            {
                "$or": [
                    {"next_post_at": {"$exists": False}},
                    {"next_post_at": None},
                    {"next_post_at": {"$lte": now}},
                ]
            },
        ],
    }
    return [item async for item in repost_pairs.find(query)]


async def complete_queued_repost(source, target, message_id, interval_hours):
    next_post_at = _now() + timedelta(hours=max(0, int(interval_hours)))
    await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        {
            "$pull": {"pending_message_ids": int(message_id)},
            "$inc": {"processed": 1},
            "$set": {
                "last_post_at": _now(),
                "last_error": None,
                "next_post_at": next_post_at,
            },
        },
    )


async def advance_backfill(source, target, next_message_id, end_message_id, interval_hours, posted):
    done = int(next_message_id) > int(end_message_id)
    update = {
        "backfill_cursor": int(next_message_id),
        "backfill_active": not done,
        "last_error": None,
    }
    if posted:
        update.update(
            {
                "last_post_at": _now(),
                "next_post_at": _now() + timedelta(hours=max(0, int(interval_hours))),
            }
        )
    result = {"$set": update}
    if posted:
        result["$inc"] = {"processed": 1}
    await repost_pairs.update_one(
        {"source": int(source), "target": int(target)},
        result,
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


async def remember_join_request(channel_id, user_id):
    await database["premium_join_requests"].update_one(
        {"channel_id": int(channel_id), "user_id": int(user_id)},
        {"$set": {"requested_at": _now()}},
        upsert=True,
    )


async def has_join_request(channel_id, user_id):
    return bool(
        await database["premium_join_requests"].find_one(
            {"channel_id": int(channel_id), "user_id": int(user_id)},
            {"_id": 1},
        )
    )


async def remove_force_sub_channel(channel_id):
    channels = await get_force_sub_channels()
    remaining = [item for item in channels if int(item["id"]) != int(channel_id)]
    if len(remaining) == len(channels):
        return 0
    await set_setting("force_sub_channels", remaining)
    return 1
