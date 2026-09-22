"""src/bot.py — Vercel entry point.

This slim module re-exports the shared bot surface from the modular components
so the existing Vercel API handlers (api/webhook.py, api/cron.py) keep working
without change. All real logic lives in src/handlers.py, src/aggregator.py,
src/search.py and src/utils.py.
"""

from src.handlers import GoodreadsBot, get_bot
from src.aggregator import MultiSourceBookAggregator

__all__ = ["GoodreadsBot", "get_bot", "MultiSourceBookAggregator"]