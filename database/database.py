import motor.motor_asyncio
from datetime import datetime, timedelta
from config import DB_URI, DB_NAME

dbclient = motor.motor_asyncio.AsyncIOMotorClient(DB_URI)
database = dbclient[DB_NAME]

user_data = database['users']

default_verify = {
    'is_verified': False,
    'verified_time': 0,
    'verify_token': "",
    'link': ""
}

def new_user(id):
    return {
        '_id': id,
        'verify_status': {
            'is_verified': False,
            'verified_time': "",
            'verify_token': "",
            'link': ""
        },
        'premium': False,
        'premium_expiry': None,
        'premium_reminder_sent': False,
    }

async def present_user(user_id: int):
    found = await user_data.find_one({'_id': user_id})
    return bool(found)

async def add_user(user_id: int):
    user = new_user(user_id)
    await user_data.insert_one(user)
    return

async def db_verify_status(user_id):
    user = await user_data.find_one({'_id': user_id})
    if user:
        return user.get('verify_status', default_verify)
    return default_verify

async def db_update_verify_status(user_id, verify):
    await user_data.update_one({'_id': user_id}, {'$set': {'verify_status': verify}})

async def full_userbase():
    user_docs = user_data.find()
    user_ids = [doc['_id'] async for doc in user_docs]
    return user_ids

async def del_user(user_id: int):
    await user_data.delete_one({'_id': user_id})
    return


async def check_premium_access(user_id: int):
    user = await user_data.find_one({'_id': int(user_id)})
    if not user or not user.get('premium'):
        return False
    expiry = user.get('premium_expiry')
    if not expiry or expiry <= datetime.utcnow():
        await user_data.update_one(
            {'_id': int(user_id)},
            {'$set': {'premium': False, 'premium_expiry': None, 'premium_reminder_sent': False}},
        )
        return False
    return expiry


async def give_premium(user_id: int, days: int):
    user_id = int(user_id)
    user = await user_data.find_one({'_id': user_id}) or {}
    current = user.get('premium_expiry')
    start = current if current and current > datetime.utcnow() else datetime.utcnow()
    expiry = start + timedelta(days=int(days))
    await user_data.update_one(
        {'_id': user_id},
        {
            '$set': {
                'premium': True,
                'premium_expiry': expiry,
                'premium_reminder_sent': False,
            }
        },
        upsert=True,
    )
    return expiry


async def remove_premium(user_id: int):
    result = await user_data.update_one(
        {'_id': int(user_id)},
        {'$set': {'premium': False, 'premium_expiry': None, 'premium_reminder_sent': False}},
    )
    return result.modified_count


pending_plans = database['premium_pending_plans']
payment_logs = database['premium_payments']


async def set_pending_plan(user_id: int, days: int, price: int):
    await pending_plans.update_one(
        {'_id': int(user_id)},
        {
            '$set': {
                'days': int(days),
                'price': int(price),
                'status': 'selected',
                'created_at': datetime.utcnow(),
            }
        },
        upsert=True,
    )


async def get_pending_plan(user_id: int):
    return await pending_plans.find_one({'_id': int(user_id)})


async def update_pending_plan(user_id: int, **values):
    await pending_plans.update_one({'_id': int(user_id)}, {'$set': values})


async def clear_pending_plan(user_id: int):
    return (await pending_plans.delete_one({'_id': int(user_id)})).deleted_count


async def add_payment_log(user_id, username, plan_days, amount, status, reviewed_by=None):
    await payment_logs.insert_one(
        {
            'user_id': int(user_id),
            'username': username,
            'plan_days': int(plan_days),
            'amount': int(amount),
            'status': str(status),
            'reviewed_by': reviewed_by,
            'date': datetime.utcnow(),
        }
    )


async def premium_expiry_candidates(now, reminder_until):
    reminders = user_data.find(
        {
            'premium': True,
            'premium_expiry': {'$gt': now, '$lte': reminder_until},
            'premium_reminder_sent': {'$ne': True},
        }
    )
    expired = user_data.find({'premium': True, 'premium_expiry': {'$lte': now}})
    return [item async for item in reminders], [item async for item in expired]


async def mark_premium_reminder_sent(user_id):
    await user_data.update_one({'_id': int(user_id)}, {'$set': {'premium_reminder_sent': True}})
