from pyrogram import filters

from premium.storage import is_deeplink_admin


async def _deeplink_admin(_, __, update):
    user = getattr(update, "from_user", None)
    return bool(user and await is_deeplink_admin(user.id))


deeplink_admin = filters.create(_deeplink_admin, "DeeplinkAdminFilter")
