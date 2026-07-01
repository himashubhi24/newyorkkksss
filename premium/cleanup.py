import asyncio


async def delete_later(message, delay=300):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


def schedule_delete(*messages, delay=300):
    for message in messages:
        if message:
            asyncio.create_task(delete_later(message, delay))
