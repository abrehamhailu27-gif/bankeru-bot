"""
Configuration for the BANKERU bot.
"""

import os
from dotenv import load_dotenv

# Automatically load key-value pairs from .env file into os.environ
load_dotenv(override=True)

# Bot Token (matches Render environment variable VILLAGE_CARD_BOT_TOKEN)
BOT_TOKEN = (
    os.environ.get("VILLAGE_CARD_BOT_TOKEN") 
    or os.environ.get("TELEGRAM_BOT_TOKEN") 
    or os.environ.get("BOT_TOKEN", "")
)

# Database URL (matches Render environment variable DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Admin Telegram IDs (Hardcoded primary admin ID 442792537 + env fallbacks)
_admin_ids_raw = os.environ.get("VILLAGE_CARD_ADMIN_IDS") or os.environ.get("ADMIN_TELEGRAM_ID", "")
ADMIN_TELEGRAM_IDS = {442792537} | {
    int(x.strip()) for x in _admin_ids_raw.replace(";", ",").split(",") if x.strip().isdigit()
}

# The Telegram group chat ID used for payment receipts / support (Optional)
PAYMENTS_SUPPORT_CHAT_ID = os.environ.get("VILLAGE_CARD_SUPPORT_CHAT_ID")
if PAYMENTS_SUPPORT_CHAT_ID and PAYMENTS_SUPPORT_CHAT_ID.strip().lstrip("-").isdigit():
    PAYMENTS_SUPPORT_CHAT_ID = int(PAYMENTS_SUPPORT_CHAT_ID)
else:
    PAYMENTS_SUPPORT_CHAT_ID = None

# Where players should send manual payments - shown by /deposit
ADMIN_PAYMENT_INSTRUCTIONS = os.environ.get(
    "VILLAGE_CARD_PAYMENT_INSTRUCTIONS",
    "Please contact the admin directly for payment details.",
)

# Startup verification check
if not BOT_TOKEN:
    raise ValueError("Set VILLAGE_CARD_BOT_TOKEN in your environment variables before running.")