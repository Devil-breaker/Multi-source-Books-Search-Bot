#!/usr/bin/env python3
"""Debug script to compare book objects between normal and inline selection."""

import asyncio
import json
import logging
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from src.handlers import GoodreadsBot
from src.aggregator import MultiSourceBookAggregator

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# Patch the format_book_message to log the book object
original_format_book_message = GoodreadsBot.format_book_message

def debug_format_book_message(self, book):
    """Debug wrapper that logs the book object before formatting."""
    logger.debug(f"=== BOOK OBJECT PASSED TO format_book_message ===")
    logger.debug(json.dumps(book, indent=2, default=str))
    logger.debug("=" * 50)
    return original_format_book_message(self, book)

GoodreadsBot.format_book_message = debug_format_book_message


# Also patch the _build_inline_photo_caption to see what it gets
original_build_inline_photo_caption = GoodreadsBot._build_inline_photo_caption

def debug_build_inline_photo_caption(self, book):
    """Debug wrapper that logs the book object before building inline caption."""
    logger.debug(f"=== BOOK OBJECT PASSED TO _build_inline_photo_caption ===")
    logger.debug(json.dumps(book, indent=2, default=str))
    logger.debug("=" * 50)
    return original_build_inline_photo_caption(self, book)

GoodreadsBot._build_inline_photo_caption = debug_build_inline_photo_caption


# Patch _ensure_ratings to see what comes in and goes out
original_ensure_ratings = MultiSourceBookAggregator._ensure_ratings

@staticmethod
def debug_ensure_ratings(book):
    """Debug wrapper that logs book before and after _ensure_ratings."""
    logger.debug(f"=== BOOK OBJECT PASSED TO _ensure_ratings (IN) ===")
    logger.debug(json.dumps(book, indent=2, default=str))
    logger.debug("-" * 30)

    result_book, hc_data = original_ensure_ratings(book)

    logger.debug(f"=== BOOK OBJECT RETURNED FROM _ensure_ratings (OUT) ===")
    logger.debug(json.dumps(result_book, indent=2, default=str))
    logger.debug(f"HCDATA: {hc_data}")
    logger.debug("=" * 50)

    return result_book, hc_data

MultiSourceBookAggregator._ensure_ratings = debug_ensure_ratings


# Patch _ensure_cover to see what comes in and goes out
original_ensure_cover = MultiSourceBookAggregator._ensure_cover

@staticmethod
def debug_ensure_cover(book, hc_data=None):
    """Debug wrapper that logs book before and after _ensure_cover."""
    logger.debug(f"=== BOOK OBJECT PASSED TO _ensure_cover (IN) ===")
    logger.debug(json.dumps(book, indent=2, default=str))
    logger.debug(f"HCDATA IN: {hc_data}")
    logger.debug("-" * 30)

    result_book = original_ensure_cover(book, hc_data)

    logger.debug(f"=== BOOK OBJECT RETURNED FROM _ensure_cover (OUT) ===")
    logger.debug(json.dumps(result_book, indent=2, default=str))
    logger.debug("=" * 50)

    return result_book

MultiSourceBookAggregator._ensure_cover = debug_ensure_cover


if __name__ == "__main__":
    print("Debug script loaded. Import this in your test or run the bot normally.")
    print("Look for debug logs with '===' markers to see book objects at each stage.")