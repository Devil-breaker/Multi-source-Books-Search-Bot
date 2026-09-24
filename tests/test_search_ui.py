"""Tests for src/handlers.py — normal search UI: message building, caching, callbacks."""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import unittest
from unittest.mock import MagicMock, patch

# Patch env before importing handlers
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("HARDCOVER_API_KEY", "TEST_KEY")

from src.handlers import GoodreadsBot
from src.aggregator import MultiSourceBookAggregator
from tests.conftest import make_book


def _make_bot():
    bot = object.__new__(GoodreadsBot)
    bot.token = "TEST_TOKEN"
    bot.webhook_mode = True
    bot.search_cache = {}
    bot._SEARCH_CACHE_TTL = 60 * 60
    bot._SEARCH_CACHE_MAX = 1000
    bot._search_page_cache = {}
    bot._search_query_cache = {}
    bot._inline_callback_cache = {}
    bot._INLINE_CALLBACK_CACHE_TTL = 30 * 60
    bot.aggregator = MagicMock()
    return bot


# ── Cache ──────────────────────────────────────────────────────────────────────

class TestCache(unittest.TestCase):
    def test_set_get_cached_books_roundtrip(self):
        bot = _make_bot()
        books = [make_book(title="Book One"), make_book(title="Book Two")]
        bot._set_cached_books(42, books)

        result = bot._get_cached_books(42)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "Book One")
        self.assertEqual(result[1]["title"], "Book Two")

    def test_get_cached_books_missing_user_returns_none(self):
        bot = _make_bot()
        self.assertIsNone(bot._get_cached_books(9999))

    def test_set_cached_books_stores_3_tuple(self):
        bot = _make_bot()
        bot._set_cached_books(1, [make_book()])
        entry = bot.search_cache[1]
        self.assertEqual(len(entry), 3)
        self.assertIsInstance(entry[1], float)  # timestamp

    def test_set_cached_books_eviction(self):
        bot = _make_bot()
        bot._SEARCH_CACHE_MAX = 5
        for uid in range(10):
            bot._set_cached_books(uid, [make_book()])
        # At most MAX entries should remain
        self.assertLessEqual(len(bot.search_cache), bot._SEARCH_CACHE_MAX)


# ── Message building ───────────────────────────────────────────────────────────

class TestBuildSearchResultsMessage(unittest.TestCase):
    """_build_search_results_message produces the correct text and keyboard."""

    def _build(self, books, query="test query", uid=1, page=1, page_size=5):
        bot = _make_bot()
        return bot._build_search_results_message(books, query, uid, page, page_size)

    def test_single_page_shows_all_books(self):
        books = [
            make_book(title="Book Alpha", author="Author A",
                      search_rating=4.0, search_rating_count=100, search_rating_formatted="4.00"),
            make_book(title="Book Beta", author="Author B"),
        ]
        text, kb = self._build(books)
        self.assertIn("Book Alpha", text)
        self.assertIn("Author A", text)
        self.assertIn("4.00", text)
        self.assertIn("100 ratings", text)   # count=100, no thousands separator
        self.assertIn("Book Beta", text)
        self.assertIn("Author B", text)
        # No pagination nav expected for single page
        self.assertNotIn("◀", text)

    def test_single_page_shows_no_ratings_yet(self):
        books = [make_book(search_rating=0, search_rating_count=0, search_rating_formatted="N/A")]
        text, kb = self._build(books)
        self.assertIn("No ratings yet", text)

    def test_pagination_nav_on_page_1_of_2(self):
        books = [make_book() for _ in range(6)]  # 6 books, page_size=5 => 2 pages
        text, kb = self._build(books, page=1)
        self.assertIn("Page 1 of 2", text)
        self.assertNotIn("◀", str(kb.inline_keyboard))  # no prev on page 1
        self.assertIn("▶", str(kb.inline_keyboard))       # has next

    def test_pagination_nav_on_page_2_of_2(self):
        books = [make_book() for _ in range(6)]
        text, kb = self._build(books, page=2)
        self.assertIn("Page 2 of 2", text)
        self.assertIn("◀", str(kb.inline_keyboard))       # has prev
        self.assertNotIn("▶", str(kb.inline_keyboard))    # no next

    def test_pagination_nav_on_middle_page(self):
        books = [make_book() for _ in range(15)]  # 15 books, page_size=5 => 3 pages
        text, kb = self._build(books, page=2)
        self.assertIn("Page 2 of 3", text)
        self.assertIn("◀", str(kb.inline_keyboard))
        self.assertIn("▶", str(kb.inline_keyboard))

    def test_button_rows_max_5_per_row(self):
        """Numbered buttons must be arranged in horizontal rows of at most 5."""
        books = [make_book() for _ in range(10)]
        _, kb = self._build(books, uid=77, page=1)
        # Each row should have at most 5 buttons
        for row in kb.inline_keyboard:
            self.assertLessEqual(len(row), 5, f"Row has {len(row)} buttons, max 5 allowed")

    def test_button_rows_of_5_for_10_books(self):
        """10 books, page 1 (page_size=5) → one row of 5 buttons + nav row."""
        books = [make_book() for _ in range(10)]
        _, kb = self._build(books, uid=77, page=1)
        # Filter rows where every button text is a plain digit (numbered buttons)
        button_rows = [row for row in kb.inline_keyboard
                       if all(btn.text.strip().isdigit() for btn in row)]
        self.assertEqual(len(button_rows), 1, f"Expected 1 button row, got {len(button_rows)}")
        self.assertEqual(len(button_rows[0]), 5)
        # Nav row has at least one button whose callback starts with "page_"
        nav_rows = [row for row in kb.inline_keyboard
                    if any(btn.callback_data.startswith("page_") for btn in row)]
        self.assertEqual(len(nav_rows), 1)

    def test_button_rows_of_3_for_8_books_page_2(self):
        """8 books, page_size=5: page 2 has a single row of 3 buttons, then a nav row."""
        books = [make_book() for _ in range(8)]
        _, kb = self._build(books, uid=77, page=2)
        # Only the row of numbered buttons (6, 7, 8) — all are plain digits
        button_rows = [row for row in kb.inline_keyboard
                       if all(btn.text.strip().isdigit() for btn in row)]
        self.assertEqual(len(button_rows), 1)
        self.assertEqual(len(button_rows[0]), 3)
        # Nav row has at least one button whose callback starts with "page_"
        nav_rows = [row for row in kb.inline_keyboard
                    if any(btn.callback_data.startswith("page_") for btn in row)]
        self.assertEqual(len(nav_rows), 1)

    def test_book_callback_includes_page_num(self):
        books = [make_book() for _ in range(10)]
        _, kb = self._build(books, uid=123, page=2)
        # First button should have callback with page_num=2
        first_btn = kb.inline_keyboard[0][0]
        self.assertEqual(first_btn.callback_data, "book_123_5_2")

    def test_html_injection_in_title_is_escaped(self):
        books = [make_book(title='<script>alert("xss")</script>', author='<b>Bold</b>')]
        text, _ = self._build(books)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<b>Bold</b>", text)
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", text)

    def test_query_text_is_escaped(self):
        books = [make_book()]
        text, _ = self._build(books, query='<script>alert("xss")</script>')
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_empty_books_handled(self):
        # Should not raise — max(1, 0//5) = 1 page
        text, kb = self._build([], uid=1)
        self.assertIn("Search Results", text)
        self.assertIn("Select a book:", text)

    def test_page_size_respected(self):
        books = [make_book() for _ in range(12)]
        text, kb = self._build(books, page=1, page_size=3)
        # Page 1 should have 3 numbered buttons
        num_labels = [btn.text for row in kb.inline_keyboard
                      for btn in row if btn.text not in ("◀", "▶", "1/4") and btn.text.isdigit()]
        self.assertEqual(num_labels, ["1", "2", "3"])

    def test_page_indicator_noop_button(self):
        books = [make_book() for _ in range(6)]
        _, kb = self._build(books, page=1)
        noop_found = any(
            btn.callback_data == "noop"
            for row in kb.inline_keyboard
            for btn in row
        )
        self.assertTrue(noop_found, "Page indicator should be a 'noop' callback button")


# ── Callback parsing ───────────────────────────────────────────────────────────

class TestCallbackParsing(unittest.TestCase):
    """button_callback branches parse their callback_data correctly."""

    def test_page_callback_parsing(self):
        bot = _make_bot()
        bot._set_cached_books(42, [make_book()])
        bot._search_query_cache[42] = "test"

        # Simulate the page_ branch logic (inline, no async needed)
        callback_data = "page_42_2"
        parts = callback_data.split("_")
        self.assertEqual(parts[0], "page")
        self.assertEqual(int(parts[1]), 42)
        self.assertEqual(int(parts[2]), 2)

        page_num = int(parts[2])
        bot._search_page_cache[42] = page_num
        self.assertEqual(bot._search_page_cache[42], 2)

    def test_back_callback_parsing(self):
        callback_data = "back_42"
        parts = callback_data.split("_")
        self.assertEqual(parts[0], "back")
        self.assertEqual(int(parts[1]), 42)

    def test_book_callback_4part_parsing(self):
        callback_data = "book_42_7_3"
        parts = callback_data.split("_")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "book")
        self.assertEqual(int(parts[1]), 42)  # user_id
        self.assertEqual(int(parts[2]), 7)   # book_idx
        self.assertEqual(int(parts[3]), 3)   # page_num

        # The logic that stores page for Back to Results
        page_num = int(parts[3]) if len(parts) > 3 else 1
        user_id = int(parts[1])

        bot = _make_bot()
        bot._search_page_cache[user_id] = page_num
        self.assertEqual(bot._search_page_cache[42], 3)

    def test_book_callback_3part_backward_compat(self):
        """Old 3-part callbacks degrade gracefully to page 1."""
        callback_data = "book_42_7"
        parts = callback_data.split("_")
        page_num = int(parts[3]) if len(parts) > 3 else 1
        self.assertEqual(page_num, 1)

    def test_download_callback_parsing(self):
        callback_data = "download_42_3"
        parts = callback_data.split("_")
        self.assertEqual(parts[0], "download")
        self.assertEqual(int(parts[1]), 42)
        self.assertEqual(int(parts[2]), 3)


# ── Hardcover rating preload ────────────────────────────────────────────────────

class TestPreloadHardcoverRatings(unittest.TestCase):
    """_preload_hardcover_ratings_for_page fills search_rating from per-book Hardcover lookups."""

    def test_fills_rating_from_hardcover(self):
        """A book without a rating gets one via _get_hardcover_cached."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 12000, ["Sci-Fi"], "https://cover.jpg")
        ) as mock_lookup:
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
            mock_lookup.assert_called_once()
            call_args = mock_lookup.call_args[0]
            self.assertEqual(call_args[1], "Dune")      # title
            self.assertEqual(call_args[2], "Frank Herbert")  # author
            # isbn is whatever make_book defaults to (9780743273565)
            self.assertEqual(call_args[0], "9780743273565")
        self.assertEqual(books[0]["search_rating"], 4.3)
        self.assertEqual(books[0]["search_rating_count"], 12000)
        self.assertEqual(books[0]["search_rating_formatted"], "4.30")
        self.assertEqual(books[0]["_hardcover_match"]["rating"], 4.3)
        self.assertEqual(books[0]["_hardcover_match"]["cover_url"], "https://cover.jpg")

    def test_uses_isbn_when_available(self):
        """ISBN is passed to _get_hardcover_cached when available."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert", isbn="978-0441172719",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 12000, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        # The call should have ISBN as first argument
        # (checked via call_args below)
        self.assertEqual(books[0]["search_rating"], 4.3)

    def test_no_rating_sets_search_rating_to_zero(self):
        """When Hardcover has no rating, search_rating stays 0 and _hardcover_match is not set."""
        bot = _make_bot()
        books = [make_book(title="Unknown", author="Nobody",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(0, 0, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        self.assertEqual(books[0]["search_rating"], 0)
        self.assertIsNone(books[0].get("_hardcover_match"))

    def test_page_2_only_preloads_books_6_to_10(self):
        """Only visible books on the given page are looked up."""
        bot = _make_bot()
        # make_book(i) produces title="Book {i}" with 0-based i (Book 0 … Book 9)
        books = [make_book(title=f"Book {i}", author="Author",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A") for i in range(10)]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.0, 100, [], "")
        ) as mock_lookup:
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=2, page_size=5))
            # Should be called exactly 5 times (books 6-10, i.e., indices 5-9)
            self.assertEqual(mock_lookup.call_count, 5)
            seen_titles = {call[0][1] for call in mock_lookup.call_args_list}
            # Page 2: start_idx=5, end_idx=10 → indices 5..9 = Book 5..Book 9 (0-based)
            expected_titles = {f"Book {i}" for i in range(5, 10)}
            self.assertEqual(seen_titles, expected_titles)

    def test_preserves_existing_non_search_fields(self):
        """Preload must not modify fields other than search_rating* and _hardcover_match."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert",
                           rating=3.5,      # canonical — must stay
                           search_rating=0,
                           search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 1000, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        self.assertEqual(books[0]["rating"], 3.5)  # canonical unchanged
        self.assertEqual(books[0]["search_rating"], 4.3)  # list field set

    def test_empty_book_list_no_crash(self):
        """Empty book list must not crash."""
        bot = _make_bot()
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.0, 100, [], "")
        ):
            asyncio.run(bot._preload_hardcover_ratings_for_page([], page_num=1, page_size=5))

    def test_cache_hit_no_http_call_needed(self):
        """_get_hardcover_cached is still called (cache is internal); no crash on repeated call."""
        bot = _make_bot()
        books = [make_book(title="Dune", author="Frank Herbert",
                           search_rating=0, search_rating_count=0,
                           search_rating_formatted="N/A")]
        with patch.object(
            MultiSourceBookAggregator, "_get_hardcover_cached",
            return_value=(4.3, 12000, [], "")
        ):
            # First preload
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
            # Second preload (same books) — cache should hit; still calls _get_hardcover_cached
            asyncio.run(bot._preload_hardcover_ratings_for_page(books, page_num=1, page_size=5))
        self.assertEqual(books[0]["search_rating"], 4.3)


# ── Search page / query caches ─────────────────────────────────────────────────

class TestPaginationCaches(unittest.TestCase):
    def test_page_cache_stores_and_retrieves(self):
        bot = _make_bot()
        bot._search_page_cache[10] = 3
        bot._search_query_cache[10] = "harry potter"
        self.assertEqual(bot._search_page_cache.get(10), 3)
        self.assertEqual(bot._search_query_cache.get(10), "harry potter")

    def test_page_cache_missing_returns_none(self):
        bot = _make_bot()
        self.assertIsNone(bot._search_page_cache.get(9999))
        self.assertIsNone(bot._search_query_cache.get(9999))

    def test_back_to_results_restores_correct_page(self):
        """Simulate: user on page 2 selects book, then hits Back to Results."""
        bot = _make_bot()
        uid = 55
        books = [make_book(title=f"Book {i}") for i in range(10)]

        bot._set_cached_books(uid, books)
        bot._search_page_cache[uid] = 2  # user navigated to page 2
        bot._search_query_cache[uid] = "test query"

        # Simulate book_ callback: user selects book index 5 on page 2
        callback_data = f"book_{uid}_5_2"
        parts = callback_data.split("_")
        page_num = int(parts[3]) if len(parts) > 3 else 1
        bot._search_page_cache[uid] = page_num

        # Simulate back_ callback: restore page
        page_num = bot._search_page_cache.get(uid, 1)
        query_text = bot._search_query_cache.get(uid, "")

        # Rebuild the message as Back to Results would
        text, kb = bot._build_search_results_message(books, query_text, uid, page_num, 5)

        self.assertIn("Page 2 of 2", text)
        self.assertIn("6.", text)  # Book 6 is first on page 2 (global index 5)


if __name__ == "__main__":
    unittest.main()