"""Entry point for the Multi-Source Book Bot (polling mode)."""

import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")

from src.handlers import GoodreadsBot  # noqa: E402, F401

bot = GoodreadsBot(TELEGRAM_BOT_TOKEN)
bot.run()