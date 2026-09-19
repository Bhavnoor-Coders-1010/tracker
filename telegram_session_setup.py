"""Create a Telethon StringSession for unattended deployment.

Run this locally once after setting TELEGRAM_API_ID and TELEGRAM_API_HASH in
.env. The printed value should be stored as the TELEGRAM_SESSION_STRING secret
in Render and never committed to source control.
"""

import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id = os.environ.get("TELEGRAM_API_ID", "")
api_hash = os.environ.get("TELEGRAM_API_HASH", "")
if not api_id or not api_hash:
    raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")


with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    client.start()
    print("\nTELEGRAM_SESSION_STRING=")
    print(client.session.save())
