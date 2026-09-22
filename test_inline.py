"""Quick test — validates the inline search code paths without needing Telegram polling."""
import asyncio
import sys
import os

# Patch environment
os.environ["TELEGRAM_BOT_TOKEN"] = "TEST_TOKEN"
os.environ["HARDCOVER_API_KEY"] = "TEST_KEY"

from src.handlers import GoodreadsBot
from telegram import InlineQueryResultPhoto
from telegram.constants import ParseMode


def test_inline_photo_result_construction():
    """Verify InlineQueryResultPhoto can be constructed correctly."""
    bot = GoodreadsBot("TEST_TOKEN")

    # Simulate a book dict
    book = {
        "title": "Harry Potter and the Prisoner of Azkaban",
        "author": "J.K. Rowling",
        "rating": 4.58,
        "rating_count": 5044472,
        "published_date": "1999-05-01",
        "page_count": 547,
        "description": "Harry Potter, along with his best friends, Ron and Hermione, is about to start his third year at Hogwarts School of Witchcraft and Wizardry. Harry can't wait to get back to school after the summer holidays.",
        "source": "hardcover",
        "cover_url": "https://covers.openlibrary.org/b/isbn/9780439655484-L.jpg",
    }

    # Test caption building
    caption = bot._build_inline_photo_caption(book)
    # Remove non-ASCII characters for safe printing on Windows console
    caption_plain = ''.join(c for c in caption if ord(c) < 128)
    print("=== Compact Caption ===")
    print(caption_plain)
    print(f"\nCaption length: {len(caption)}")
    assert len(caption) < 1024, "Caption exceeds Telegram limit!"

    # Test keyboard building
    key = "inline_123_bk_abc123"
    keyboard = bot._build_inline_photo_keyboard(key)
    print("\n=== Hourglass Keyboard ===")
    # Filter out non-ASCII characters for safe printing on Windows console
    kb_str = repr(keyboard.to_dict())
    kb_plain = ''.join(c for c in kb_str if ord(c) < 128)
    print(kb_plain)

    # Test InlineQueryResultPhoto construction
    result = InlineQueryResultPhoto(
        id="test_id",
        photo_url="https://covers.openlibrary.org/b/isbn/9780439655484-L.jpg",
        thumbnail_url="https://covers.openlibrary.org/b/isbn/9780439655484-L.jpg",
        title="Harry Potter and the Prisoner of Azkaban",
        description="J.K. Rowling • ⭐⭐⭐⭐☆ 4.58/5 • 5,044,472 ratings",
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    print("\n=== InlineQueryResultPhoto (as dict) ===")
    d = result.to_dict()
    print(f"type: {d['type']}")
    print(f"id: {d['id']}")
    print(f"photo_url: {d['photo_url']}")
    print(f"thumbnail_url: {d.get('thumbnail_url', 'N/A')}")
    print(f"title: {d['title']}")
    print(f"description length: {len(d.get('description', ''))}")
    print(f"caption length: {len(d['caption'])}")
    print(f"reply_markup has inline keyboard: {'inline_keyboard' in str(d)}")

    # Test expanded caption
    expanded = bot._build_expanded_inline_caption(book)
    expanded_plain = ''.join(c for c in expanded if ord(c) < 128)
    print("\n=== Expanded Caption ===")
    print(expanded_plain[:200] + "...")
    print(f"\nExpanded caption length: {len(expanded)}")

    # Test Goodreads keyboard
    gr_keyboard = bot._build_goodreads_keyboard(book)
    print("\n=== Goodreads Keyboard ===")
    # Filter out non-ASCII characters for safe printing on Windows console
    gr_str = repr(gr_keyboard.to_dict())
    gr_plain = ''.join(c for c in gr_str if ord(c) < 128)
    print(gr_plain)

    # Test callback data flow
    callback_key = "inline_123_bk_abc123"
    bot._set_inline_callback_data(callback_key, book)
    retrieved = bot._get_inline_callback_data(callback_key)
    assert retrieved is not None, "Cache miss!"
    assert retrieved["title"] == book["title"], "Cache data mismatch!"
    print("\n[OK] Callback cache: SET and GET work correctly")

    # Test HTML escaping
    dangerous_book = {
        "title": 'Book <Script> "test" & \'more\'',
        "author": "Author <b>Bold</b> & <i>Italic</i>",
        "rating": None,
        "rating_count": 0,
        "published_date": "",
        "page_count": 0,
        "description": "Description with <script>alert('xss')</script> and & special chars",
        "source": "hardcover",
        "cover_url": "https://example.com/cover.jpg",
    }
    safe_caption = bot._build_inline_photo_caption(dangerous_book)
    assert "<script>" not in safe_caption.lower(), "XSS vulnerability!"
    assert "&lt;" in safe_caption, "HTML not escaped!"
    print("[OK] HTML escaping works correctly")

    print("\n[OK] All inline search components validated successfully!")


if __name__ == "__main__":
    test_inline_photo_result_construction()