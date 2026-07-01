#(©)CodeXBotz




import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv


load_dotenv()



def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Bot identity. Values must be supplied by the deployment environment.
TG_BOT_TOKEN = required_env("TG_BOT_TOKEN")

#Your API ID from my.telegram.org
APP_ID = int(required_env("APP_ID"))

#Your API Hash from my.telegram.org
API_HASH = required_env("API_HASH")

#Your db channel Id
CHANNEL_ID = int(required_env("CHANNEL_ID"))

#OWNER ID
OWNER_ID = int(required_env("OWNER_ID"))

#Port
PORT = os.environ.get("PORT", "8087")

#Database 
DB_URI = required_env("DATABASE_URL")
DB_NAME = os.environ.get("DATABASE_NAME", "premium_file_bot")

SHORTLINK_URL = os.environ.get("SHORTLINK_URL", "")
SHORTLINK_API = os.environ.get("SHORTLINK_API", "")
VERIFY_EXPIRE = int(os.environ.get('VERIFY_EXPIRE', 86400)) # Add time in seconds
IS_VERIFY = os.environ.get("IS_VERIFY", "False") == "True"
TUT_VID = os.environ.get("TUT_VID", "")


#force sub channel id, if you want enable force sub
FORCE_SUB_CHANNEL = int(os.environ.get("FORCE_SUB_CHANNEL", "0"))

TG_BOT_WORKERS = int(os.environ.get("TG_BOT_WORKERS", "4"))

#start message
START_MSG = os.environ.get("START_MESSAGE", "👋 Hello {first}!\nPlease also Join our backup  channel.")
try:
    ADMINS=[]
    for x in os.environ.get("ADMINS", "").split():
        ADMINS.append(int(x))
except ValueError:
        raise Exception("Your Admins list does not contain valid integers.")

#Force sub message 
FORCE_MSG = os.environ.get("FORCE_SUB_MESSAGE", "Hello {first}\n\n<b>You need to join in  my Channel/Group ")

#set your Custom Caption here, Keep None for Disable Custom Caption
CUSTOM_CAPTION = os.environ.get("CUSTOM_CAPTION", "") or None

#set True if you want to prevent users from forwarding files from bot
PROTECT_CONTENT = True if os.environ.get('PROTECT_CONTENT', "False") == "True" else False

#Set true if you want Disable your Channel Posts Share button
DISABLE_CHANNEL_BUTTON = os.environ.get("DISABLE_CHANNEL_BUTTON", None) == 'True'

BOT_STATS_TEXT = "<b>BOT UPTIME</b>\n{uptime}"
USER_REPLY_TEXT = "join "

if OWNER_ID not in ADMINS:
    ADMINS.append(OWNER_ID)

AUTO_REPOST_ENABLED = os.environ.get("AUTO_REPOST_ENABLED", "False") == "True"


def parse_premium_plans(raw):
    plans = {}
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        try:
            days, price = item.split(":", 1)
            days, price = int(days), int(price)
        except (TypeError, ValueError):
            raise RuntimeError("PREMIUM_PLANS must use DAYS:PRICE,DAYS:PRICE format")
        if days < 1 or price < 1:
            raise RuntimeError("Premium plan days and prices must be positive")
        plans[days] = price
    return plans


PREMIUM_PLANS = parse_premium_plans(os.environ.get("PREMIUM_PLANS", ""))
UPI_ID = os.environ.get("UPI_ID", "").strip()
UPI_NAME = os.environ.get("UPI_NAME", "Premium Access").strip()
PAYMENT_REVIEW_CHAT = os.environ.get("PAYMENT_REVIEW_CHAT", str(OWNER_ID)).strip()
if PAYMENT_REVIEW_CHAT.lstrip("-").isdigit():
    PAYMENT_REVIEW_CHAT = int(PAYMENT_REVIEW_CHAT)
PREMIUM_REPORT_CHAT = os.environ.get("PREMIUM_REPORT_CHAT", str(PAYMENT_REVIEW_CHAT)).strip()
if PREMIUM_REPORT_CHAT.lstrip("-").isdigit():
    PREMIUM_REPORT_CHAT = int(PREMIUM_REPORT_CHAT)
PAYMENT_EXPIRY_MINUTES = int(os.environ.get("PAYMENT_EXPIRY_MINUTES", "30"))

LOG_FILE_NAME = "filesharingbot.txt"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(levelname)s] - %(name)s - %(message)s",
    datefmt='%d-%b-%y %H:%M:%S',
    handlers=[
        RotatingFileHandler(
            LOG_FILE_NAME,
            maxBytes=50000000,
            backupCount=10
        ),
        logging.StreamHandler()
    ]
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)


def LOGGER(name: str) -> logging.Logger:
    return logging.getLogger(name)
