import asyncio


async def delete_later(message, delay=60):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


def schedule_delete(*messages, delay=60):
    for message in messages:
        if message:
            asyncio.create_task(delete_later(message, delay))
