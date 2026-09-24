"""GoodreadsBot — all Telegram command and callback handlers."""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import requests
from io import BytesIO
from PIL import Image
from urllib.parse import urlsplit

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaPhoto,
)
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, InlineQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut

from src.utils import logger, HEADERS, html_escape, is_placeholder_image, is_english_description, translate_to_english
from src.search import build_goodreads_url
from src.aggregator import MultiSourceBookAggregator


class GoodreadsBot:
    def __init__(self, token: str, webhook_mode: bool = False):
        self.token = token
        # Generous timeouts make the long-polling loop more tolerant of slow or
        # flaky networks (the source of the httpx.ReadError on getUpdates).
        self.app = (
            Application.builder()
            .token(token)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .pool_timeout(30.0)
            .get_updates_connect_timeout(30.0)
            .get_updates_read_timeout(40.0)
            .build()
        )
        # search_cache: {user_id: (books_list, timestamp)}
        # Entries expire after _SEARCH_CACHE_TTL seconds.
        self.search_cache: dict = {}
        self._SEARCH_CACHE_TTL: int = 60 * 60  # 60 minutes
        # Per-user current page and query text for normal search pagination
        self._search_page_cache: dict[int, int] = {}  # user_id -> page number
        self._search_query_cache: dict[int, str] = {}  # user_id -> query text
        self._SEARCH_CACHE_MAX: int = 1000      # max users tracked
        self.aggregator = MultiSourceBookAggregator()
        self.webhook_mode = webhook_mode
        # Per-user inline query debounce tasks.
        # Key = user_id; Value = asyncio.Task that performs the debounced search.
        self._inline_debounce_tasks: dict[int, asyncio.Task] = {}
        self._inline_debounce_lock = asyncio.Lock()
        # Inline callback cache: {callback_key: book_data}
        # Entries expire after _INLINE_CALLBACK_CACHE_TTL seconds.
        self._inline_callback_cache: dict = {}
        self._INLINE_CALLBACK_CACHE_TTL: int = 30 * 60  # 30 minutes
        self.setup_handlers()

    def _get_cached_books(self, user_id: int) -> list | None:
        """Return cached search results for *user_id*, or None if missing/expired."""
        entry = self.search_cache.get(user_id)
        if entry is None:
            return None
        ts = entry[1]
        if time.time() - ts > self._SEARCH_CACHE_TTL:
            self.search_cache.pop(user_id, None)
            return None
        return entry[0]

    def _set_cached_books(self, user_id: int, books: list) -> None:
        """Store search results for *user_id* with a timestamp."""
        # Bound cache size — drop oldest entries when full
        if len(self.search_cache) >= self._SEARCH_CACHE_MAX:
            # Evict the 10% oldest by timestamp
            sorted_users = sorted(
                self.search_cache, key=lambda k: self.search_cache[k][1]
            )
            for uid in sorted_users[: max(1, len(sorted_users) // 10)]:
                self.search_cache.pop(uid, None)
        self.search_cache[user_id] = (books, time.time(), "")

    def _get_inline_callback_data(self, callback_key: str) -> dict | None:
        """Return cached book data for *callback_key*, or None if missing/expired."""
        entry = self._inline_callback_cache.get(callback_key)
        if entry is None:
            return None
        data, ts = entry
        if time.time() - ts > self._INLINE_CALLBACK_CACHE_TTL:
            self._inline_callback_cache.pop(callback_key, None)
            return None
        return data

    def _set_inline_callback_data(self, callback_key: str, book_data: dict) -> None:
        """Store book data for *callback_key* with a timestamp."""
        # Bound cache size — drop oldest entries when full
        if len(self._inline_callback_cache) >= 1000:  # Reasonable limit for inline callbacks
            # Evict the 10% oldest by timestamp
            sorted_keys = sorted(
                self._inline_callback_cache, key=lambda k: self._inline_callback_cache[k][1]
            )
            for key in sorted_keys[: max(1, len(sorted_keys) // 10)]:
                self._inline_callback_cache.pop(key, None)
        self._inline_callback_cache[callback_key] = (book_data, time.time())

    # ── Handler registration ──────────────────────────────────────────────────

    def setup_handlers(self):
        """Register all command and callback handlers."""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("search", self.search_command))
        self.app.add_handler(CommandHandler("ping", self.ping_command))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_handler(InlineQueryHandler(self.inline_search))

    def process_update(self, raw_update: dict) -> bool:
        """Process a single update dict received from Telegram webhook.
        Returns True if processed, False otherwise.
        """
        try:
            update = Update.de_json(raw_update, self.app.bot)
            loop = None
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            coro = self.app.process_update(update)
            future = asyncio.ensure_future(coro)
            loop.run_until_complete(future)

            result = future.result()
            if isinstance(result, Exception):
                logger.error(f"Handler raised: {result}")
                return False
            return True

        except Exception as e:
            logger.error(f"Error processing update: {e}", exc_info=True)
            return False

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Central error handler for transient network errors."""
        err = context.error
        if isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"🌐 Transient network error (auto-retrying): {err!r}")
            return
        logger.error("Unhandled exception while processing update:", exc_info=err)

    # ── Commands ────────────────────────────────────────────────────────────────

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send welcome message on /start."""
        await update.message.reply_text( f"""
🤖 <b>Multi-Source Book Bot</b>

Welcome! I search across multiple sources to find the best book information,
covers, and descriptions.

<b>How to use:</b>
• <code>/search &lt;book_title&gt;</code> - Search for books
• <code>@{context.bot.username} &lt;book_name&gt;</code> - Inline search from any chat

<b>Example:</b>
<code>/search Harry Potter and the Prisoner of Azkaban</code>
<code>@{context.bot.username} Harry Potter</code>

<b>Data Sources:</b>
📚 Google Books - Descriptions & metadata
🍎 iTunes - High-resolution covers
💠 Hardcover.app - Community ratings
📖 StoryGraph - Social reading ratings

Use /help for more information.
            """ .strip(),
            parse_mode=ParseMode.HTML,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help text on /help."""
        await update.message.reply_text( f"""
<b>📚 Multi-Source Book Bot Help</b>

<b>Commands:</b>
<code>/start</code> - Show welcome message
<code>/help</code> - Show this help message
<code>/search &lt;query&gt;</code> - Search for books
<code>/ping</code> - Check if the bot is running

<b>Features:</b>
✓ Searches multiple sources simultaneously
✓ Combines best data from each source
✓ High-resolution covers from iTunes
✓ Descriptions from Google Books
✓ Community ratings from Hardcover.app
✓ Social ratings from StoryGraph
✓ Download covers as image files

<b>Inline Search:</b>
Use the bot from any Telegram chat by typing:
<code>@{context.bot.username} &lt;book_name&gt;</code>

Example: <code>@{context.bot.username} Harry Potter</code>

<b>Tips:</b>
• Use full book titles for best results
• Include author name for better matching
• Try different keywords if no results
            """ .strip(),
            parse_mode=ParseMode.HTML,
        )

    async def ping_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reply with bot status on /ping."""
        await update.message.reply_text("✅ Bot is running and polling Telegram!")

    # ── Inline search ───────────────────────────────────────────────────────────

    @staticmethod
    def _inline_title_author_match(a_title: str, a_author: str,
                                   b_title: str, b_author: str) -> bool:
        """Fast title+author matching for inline merge/dedup.

        Returns True when both title and author are plausibly the same book.
        Uses substring containment + lowered/trimmed comparison.
        """
        at = a_title.lower().strip()
        bt = b_title.lower().strip()
        if not at or not bt:
            return False
        title_ok = at in bt or bt in at
        if not title_ok:
            return False
        aa = a_author.lower().strip()
        ba = b_author.lower().strip()
        if not aa or not ba:
            return True  # can't disqualify without author data
        return aa in ba or ba in aa

    @staticmethod
    def _inline_deterministic_id(title: str, author: str, source: str,
                                 isbn: str = "", idx: int = 0) -> str:
        """Generate a deterministic, process-stable inline result ID.

        Uses SHA-1 to avoid Python's randomized hash().
        Prefers ISBN when available.
        """
        if isbn:
            return f"isbn_{isbn}"
        raw = f"{title.lower().strip()}|{author.lower().strip()}|{source}|{idx}"
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"bk_{h}"

    async def inline_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline queries with debounce.

        - Queries < 3 chars are ignored (no API call).
        - A ~800 ms debounce prevents searching on every keystroke.
        - New queries from the same user cancel any pending search.
        - Only the latest query may answer Telegram.
        - Results use InlineQueryResultArticle with compact article style and callback buttons.
        """
        query = (update.inline_query.query or "").strip()
        user_id = update.inline_query.from_user.id
        query_id = update.inline_query.id

        # ── 1. Short queries: return empty immediately ────────────────────────────
        if len(query) < 3:
            logger.debug(f"Inline query too short: '{query}' user={user_id}")
            await update.inline_query.answer([], cache_time=60, is_personal=False)
            return

        logger.info(f"Inline query received: '{query}' user={user_id}")

        # ── 2. Cancel any previous pending search for this user ───────────────────
        async with self._inline_debounce_lock:
            prev = self._inline_debounce_tasks.get(user_id)
            if prev and not prev.done():
                prev.cancel()
                logger.info(f"Inline debounce cancelled: user={user_id}")
            # Start a new debounced search task
            task = asyncio.create_task(
                self._debounced_search(query, user_id, query_id)
            )
            self._inline_debounce_tasks[user_id] = task
            task.add_done_callback(
                lambda t: self._cleanup_debounce_task(user_id, t)
            )

    def _cleanup_debounce_task(self, user_id: int, task: asyncio.Task) -> None:
        """Remove a completed/cancelled task from the debounce dict."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass  # expected for cancelled tasks
        except Exception:
            pass
        # Clean up if this task is still the one tracked for this user
        current = self._inline_debounce_tasks.get(user_id)
        if current is task:
            del self._inline_debounce_tasks[user_id]

    async def _debounced_search(self, query: str, user_id: int, query_id: str) -> None:
        """Wait ~800 ms then perform the Hardcover search. Cancelled if superseded."""
        DEBOUNCE_MS = 0.85  # seconds
        try:
            await asyncio.sleep(DEBOUNCE_MS)
        except asyncio.CancelledError:
            logger.info(f"Inline debounce cancelled: '{query}' user={user_id}")
            raise  # propagate so _cleanup_debounce_task handles it

        logger.info(f"Inline debounce completed: '{query}' user={user_id}")
        await self._do_inline_search(query, user_id, query_id)

    async def _do_inline_search(
        self, query: str, user_id: int, query_id: str
    ) -> None:
        """Perform the actual Hardcover search and send answerInlineQuery.

        Only sends if this is still the latest query for the user (not stale).
        """
        # Verify this is still the current pending query
        current_task = self._inline_debounce_tasks.get(user_id)
        if current_task is None or current_task.done():
            logger.debug(f"Inline search skipped (stale): '{query}'")
            return

        t_start = time.monotonic()
        logger.info(f"Inline search: '{query}'")

        from src.aggregator import MultiSourceBookAggregator as MSA

        # ── Phase 1: Concurrent Hardcover + iTunes ──────────────────────────────
        hc_task = asyncio.create_task(
            asyncio.to_thread(MSA.search_hardcover, query, 10)
        )
        itunes_task = asyncio.create_task(
            asyncio.to_thread(MSA.search_itunes, query)
        )

        done, _pending = await asyncio.wait(
            {hc_task, itunes_task},
            return_when=asyncio.ALL_COMPLETED,
        )

        # Collect books by source
        hc_books: list[dict] = []
        it_books: list[dict] = []
        for t in done:
            try:
                r = t.result()
                if not isinstance(r, list):
                    continue
                if r and r[0].get("source") == "hardcover":
                    hc_books.extend(r)
                else:
                    it_books.extend(r)
            except Exception:
                pass

        elapsed_fetch = time.monotonic() - t_start
        logger.info(
            f"⏱️ Inline sources fetched in {elapsed_fetch:.1f}s "
            f"(Hardcover={len(hc_books)}, iTunes={len(it_books)})"
        )

        # ── Phase 2: Merge — Hardcover primary, iTunes cover supplementation ──────
        merged: list[dict] = []
        matched_it_indices: set[int] = set()

        for hc in hc_books:
            book = hc.copy()
            for it_idx, it in enumerate(it_books):
                if it_idx in matched_it_indices:
                    continue
                if self._inline_title_author_match(
                    hc.get("title", ""), hc.get("author", ""),
                    it.get("title", ""), it.get("author", ""),
                ):
                    # Prefer iTunes cover when Hardcover lacks one
                    if it.get("cover_url") and not book.get("cover_url"):
                        book["cover_url"] = it["cover_url"]
                    matched_it_indices.add(it_idx)
                    break
            merged.append(book)

        # Unmatched iTunes-only results
        for it_idx, it in enumerate(it_books):
            if it_idx not in matched_it_indices:
                merged.append(it.copy())

        final = merged[:20]

        elapsed = time.monotonic() - t_start
        logger.info(f"⏱️ Inline total: {elapsed:.1f}s → {len(final)} results")

        # ── Phase 3: Build InlineQueryResultArticle list ─────────────────────────
        results = []
        for idx, book in enumerate(final):
            title = book.get("title", "Unknown")
            author = book.get("author", "Unknown") or "Unknown"
            isbn = (book.get("isbn") or "").replace("-", "").strip()
            cover_url = book.get("cover_url") or ""
            source = book.get("source", "unknown")

            # Skip if no cover URL (we show thumbnails; fallback to None is allowed but discouraged)
            if not cover_url:
                logger.debug(f"Skipping book '{title}' due to missing cover URL")
                continue

            result_id = self._inline_deterministic_id(
                title, author, source, isbn=isbn, idx=idx,
            )

            # Compact description: author + rating + year
            desc_parts = [author]
            year = (book.get("published_date") or "")[:4]
            rating = book.get("rating_formatted") or book.get("rating")
            rating_cnt = book.get("rating_count", 0)
            if rating and rating_cnt:
                try:
                    stars = "⭐" * min(int(float(str(rating).replace(",", "."))), 5)
                    desc_parts.append(f"{stars} {rating}/5 · {rating_cnt:,} ratings")
                except ValueError:
                    pass
            if year:
                desc_parts.append(year)
            description = " • ".join(desc_parts)

            # Store book data in callback cache for later retrieval
            callback_key = f"inline_{user_id}_{result_id}"
            self._set_inline_callback_data(callback_key, book)

            # Skip if no title (required for InlineQueryResultArticle)
            if not title or title == "Unknown":
                logger.debug(f"Skipping book due to missing title")
                continue

            result_id = self._inline_deterministic_id(
                title, author, source, isbn=isbn, idx=idx,
            )

            # Store book data in callback cache for later retrieval
            callback_key = f"inline_{user_id}_{result_id}"
            self._set_inline_callback_data(callback_key, book)

            # Build message content for when result is selected
            message_content = InputTextMessageContent(
                message_text=self._build_inline_photo_caption(book),
                parse_mode=ParseMode.HTML,
            )

            # InlineQueryResultArticle — compact list/article style for inline results
            result = InlineQueryResultArticle(
                id=result_id,
                title=title,
                description=description,
                thumbnail_url=cover_url if cover_url else None,
                input_message_content=message_content,
                reply_markup=self._build_inline_photo_keyboard(callback_key),
            )

            results.append(result)

        logger.info(f"Inline result types: {', '.join('article' for _ in results)}")

        # ── Phase 4: Send answerInlineQuery ──────────────────────────────────────
        # Check once more that this is still the active query for the user
        current_task = self._inline_debounce_tasks.get(user_id)
        if current_task is None or current_task.done():
            logger.debug(f"Inline answer skipped (stale): '{query}'")
            return

        api_url = f"https://api.telegram.org/bot{self.token}/answerInlineQuery"
        payload = {
            "inline_query_id": query_id,
            "results": [result.to_dict() for result in results],
            "cache_time": 60,
            "is_personal": False,
            "next_offset": "",
        }
        try:
            resp = requests.post(api_url, json=payload, timeout=5)
            if resp.status_code != 200 or not resp.json().get("ok"):
                logger.warning(f"answerInlineQuery failed: {resp.text}")
            else:
                logger.info(f"Inline answer sent: '{query}' → {len(results)} results")
        except Exception as e:
            logger.warning(f"answerInlineQuery error: {e}")

    # ── Inline photo helpers ────────────────────────────────────────────────────

    def _build_inline_photo_caption(self, book: dict) -> str:
        """Build compact caption for inline photo results (initial selection)."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        isbn = html_escape(book.get("isbn", ""))
        pages = book.get("page_count", 0)
        year = (book.get("published_date") or "")[:4]
        rating = book.get("rating_formatted") or book.get("rating")
        rating_cnt = book.get("rating_count", 0)

        parts = [
            f"📖 Title: <b>{title}</b>",
            f"✍️ Author: {author}",
        ]

        categories = book.get("categories", [])
        if categories:
            genres_str = ", ".join(categories[:5])
            parts.append(f"🏷️ Genres: {html_escape(genres_str)}")

        if rating and rating_cnt:
            try:
                rating_num = float(str(rating).replace(",", "."))
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(
                    f"📊 Rating: {stars} <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings)"
                )
            except ValueError:
                parts.append(
                    f"📊 Rating: <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings)"
                )

        if isbn:
            parts.append(f"📚 ISBN: <b>{isbn}</b>")
        if pages:
            parts.append(f"📄 Pages: <b>{pages}</b>")
        if year:
            parts.append(f"📅 Year: <b>{year}</b>")

        desc_text = (book.get("description") or "").strip()
        if desc_text:
            desc_text = re.sub(r"<[^>]+>", "", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            if len(desc_text) > 100:
                desc_text = desc_text[:97] + "..."
            desc_text = html_escape(desc_text)
            parts.append("")
            parts.append("📄 Summary")
            parts.append(f"&gt; {desc_text}")

        source = book.get("source", "unknown").replace("_", " ").title()
        parts.append("")
        parts.append(f"🔵 Source: {source}")

        return "\n".join(parts)

    def _build_inline_photo_keyboard(self, callback_key: str) -> InlineKeyboardMarkup:
        """Build inline keyboard for photo results with hourglass button."""
        keyboard = [[InlineKeyboardButton("⏳", callback_data=f"hourglass_{callback_key}")]]
        return InlineKeyboardMarkup(keyboard)

    def _build_expanded_inline_caption(self, book: dict) -> str:
        """Build expanded caption for when hourglass button is pressed."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        isbn = html_escape(book.get("isbn", ""))
        pages = book.get("page_count", 0)
        year = (book.get("published_date") or "")[:4]
        lang = book.get("language", "")
        publisher = html_escape(book.get("publisher", ""))

        parts = [
            f"📖 <b>Title:</b> {title}",
            f"✍️ <b>Author:</b> {author}",
        ]

        # Genres
        categories = book.get("categories", [])
        if categories:
            genres_str = ", ".join(categories)
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Rating
        rating = book.get("rating_formatted") or book.get("rating")
        rating_cnt = book.get("rating_count", 0)
        rating_reviews = book.get("rating_reviews", 0)
        if rating and rating_cnt:
            try:
                rating_num = float(str(rating).replace(",", "."))
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(
                    f"📊 <b>Rating:</b> {stars} <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )
            except ValueError:
                parts.append(
                    f"📊 <b>Rating:</b> {html_escape(str(rating))}/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )

        if year:
            parts.append(f"📅 <b>Published:</b> {year}")
        if lang:
            parts.append(f"🌐 <b>Language:</b> {html_escape(lang)}")
        if publisher:
            parts.append(f"🏢 <b>Publisher:</b> {publisher}")
        if pages:
            parts.append(f"📚 <b>Format:</b> {pages} pages")
        if isbn:
            parts.append(f"🆔 <b>ISBN:</b> <code>{isbn}</code>")

        # ASIN if available
        asin = book.get("asin", "")
        if asin:
            parts[-1] = parts[-1].replace("</code>", f" | <b>ASIN:</b> {html_escape(asin)}</code>")

        parts.append("")  # blank line

        # Description with expandable blockquote
        desc_text = (book.get("description") or "").strip()
        if desc_text:
            # Clean HTML tags
            desc_text = re.sub(r"<[^>]+>", "", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            # Escape for HTML
            desc_text = html_escape(desc_text)
            # Truncate plain text to safe length BEFORE wrapping in HTML
            max_desc_length = 800  # Conservative limit for description
            if len(desc_text) > max_desc_length:
                # Truncate at word boundary
                truncated = desc_text[:max_desc_length]
                last_space = truncated.rfind(" ")
                if last_space > max_desc_length * 0.8:
                    desc_text = truncated[:last_space] + "..."
                else:
                    desc_text = truncated + "..."
            parts.append("")
            parts.append("📄 <b>Summary</b>")
            parts.append(f"<blockquote expandable>{desc_text}</blockquote>")

        parts.append("")
        parts.append("🔵 <b>Source:</b> {source}".format(source=book.get("source", "unknown").replace('_', ' ').title()))

        # Join and ensure length is safe
        caption = "\n".join(parts)
        # Final safety check - if still too long, remove optional fields
        if len(caption) > 1020:
            # Remove ASIN line if present
            if asin:
                for i, part in enumerate(parts):
                    if "ASIN:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove publisher
            if len(caption) > 1020 and publisher:
                for i, part in enumerate(parts):
                    if "Publisher:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove language
            if len(caption) > 1020 and lang:
                for i, part in enumerate(parts):
                    if "Language:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove published year
            if len(caption) > 1020 and year:
                for i, part in enumerate(parts):
                    if "Published:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove pages
            if len(caption) > 1020 and pages:
                for i, part in enumerate(parts):
                    if "Format:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # Last resort: truncate description further
            if len(caption) > 1020:
                for i, part in enumerate(parts):
                    if part == "📄 <b>Summary</b>":
                        if i + 1 < len(parts) and parts[i + 1].startswith("<blockquote expandable>"):
                            current_desc = parts[i + 1][23:-13]
                            parts_without_desc = parts[:i+1] + [""] + parts[i+2:]
                            base_length = len("\n".join(parts_without_desc))
                            max_desc_len = 1020 - base_length - 3
                            if max_desc_len > 10:
                                if len(current_desc) > max_desc_len:
                                    truncated = current_desc[:max_desc_len]
                                    last_space = truncated.rfind(" ")
                                    if last_space > max_desc_len * 0.8:
                                        truncated = truncated[:last_space]
                                    parts[i + 1] = f"<blockquote expandable>{truncated}...</blockquote>"
                            break
                caption = "\n".join(parts)

        return caption

    def _build_goodreads_keyboard(self, book: dict) -> InlineKeyboardMarkup:
        """Build keyboard with Goodreads button for expanded view."""
        gr_url = build_goodreads_url(book)
        keyboard = [[InlineKeyboardButton("📚 Open Goodreads 🔗", url=gr_url)]]
        return InlineKeyboardMarkup(keyboard)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def download_and_save_image(self, cover_url: str, book: dict = None):
        """Download cover image and save to a temp file. Returns path or None.

        Args:
            cover_url: URL to download.
            book: Book dict (optional). Used for cover fallback and diagnostics.
                  When the primary cover is a placeholder, retries with the
                  Hardcover cover from book["_hardcover_match"]["cover_url"] if available.
        """
        cover_source = (book or {}).get("cover_source", "unknown")

        def _log_cover_diagnostic(url, status, ctype, clen, final_url, width, height):
            """Log diagnostic info for a cover download (PART 4)."""
            domain = urlsplit(url).netloc
            final_domain = urlsplit(final_url).netloc if final_url != url else domain
            logger.info(
                f"Cover diag: source={cover_source} url={url[:70]} "
                f"status={status} type={ctype} len={clen} "
                f"domain={final_domain} dims={width}x{height}"
            )

        def _download_one(url: str):
            """Attempt one cover download. Returns (bytes, status, ctype, clen, final_url, width, height)."""
            response = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
            response.raise_for_status()
            final_url = response.url
            ctype = response.headers.get("Content-Type", "")
            clen = len(response.content)
            width = height = None
            try:
                img = Image.open(BytesIO(response.content))
                width, height = img.size
            except Exception:
                pass
            _log_cover_diagnostic(url, response.status_code, ctype, clen, final_url, width, height)
            return response.content, response.status_code, ctype, clen, final_url, width, height

        try:
            title = (book or {}).get("title", "")
            logger.info(f"📥 Downloading cover: {cover_url[:60]}... source={cover_source} title={title}")
            content, status, ctype, clen, final_url, width, height = _download_one(cover_url)

            if is_placeholder_image(content):
                logger.warning(f"⚠️ Cover placeholder detected: source={cover_source} title={title}")
                # ── PART 6 fallback: try Hardcover cover if available ─────────
                if book and cover_source == "google_books":
                    hc_cover = book.get("_hardcover_match", {}).get("cover_url")
                    if hc_cover:
                        logger.info(f"Cover fallback: source=hardcover title={title}")
                        try:
                            content, status, ctype, clen, final_url, width, height = (
                                _download_one(hc_cover)
                            )
                            if is_placeholder_image(content):
                                logger.warning(
                                    f"⚠️ Fallback cover also placeholder; skipping. title={title}"
                                )
                                return None
                            logger.info(f"✅ Cover fallback OK: source=hardcover title={title} bytes={clen}")
                            # Fall through — content now holds the valid fallback bytes.
                        except Exception as e:
                            logger.warning(f"Fallback cover download failed: {e} title={title}")
                            return None
                    else:
                        logger.info(f"No Hardcover cover available for fallback. title={title}")
                return None

            # ── Write temp file (reached by both primary and fallback paths) ──────
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            temp_file.write(content)
            temp_file.close()
            logger.info(f"✅ Downloaded: source={cover_source} title={title} bytes={clen}")
            return temp_file.name
        except Exception as e:
            logger.error(f"Error downloading image: {e}")
            return None

    def cleanup_temp_file(self, file_path: str):
        """Delete a temporary file, silently ignoring errors."""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up: {e}")

    def format_book_message(self, book: dict) -> str:
        """Format book info for display using Telegram HTML."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        rating = html_escape(str(book.get("rating_formatted", book.get("rating", "N/A"))))
        rating_cnt = book.get("rating_count", 0)
        isbn = html_escape(book.get("isbn", ""))
        pages = str(book.get("page_count", 0))
        year = book.get("published_date", "")[:4]
        desc = (book.get("description") or "").strip()

        # Clean HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()

        # Truncate to ~800 chars at a word boundary
        if len(desc) > 800:
            cutoff = desc.rfind(" ", 0, 800)
            if cutoff > 100:
                desc = desc[:cutoff] + "..."
            else:
                desc = desc[:800] + "..."

        desc_escaped = html_escape(desc)

        # Source badge
        gr_enhanced = book.get("gr_enhanced", False)
        cover_source = book.get("cover_source", book.get("source", "unknown"))
        source_emoji = {
            "google_books": "🔵",
            "open_library": "🟢",
            "itunes": "🟠",
            "goodreads": "🔴",
        }.get(cover_source, "⚪")
        source_text = "Goodreads" if gr_enhanced else cover_source.replace("_", " ").title()

        parts = []

        # Header
        parts.append(f"<b>{title}</b>")
        parts.append(f"<i>{author}</i>")

        # Rating line with stars
        if rating_cnt > 0:
            try:
                rating_for_float = str(book.get("rating", "")).replace(",", ".")
                rating_num = float(rating_for_float)
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(f"{stars} <b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
            except ValueError:
                parts.append(f"<b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
        else:
            parts.append("❓ <b>No ratings yet</b>")

        parts.append("")  # blank line

        # Metadata
        if isbn:
            parts.append(f"📖 <b>ISBN:</b> <code>{isbn}</code>")
        if pages and int(pages) > 0:
            parts.append(f"📄 <b>Pages:</b> <code>{pages}</code>")
        if year:
            parts.append(f"📅 <b>Year:</b> <code>{year}</code>")

        # Genres
        categories = book.get("categories", [])
        if categories:
            genres_str = ", ".join(categories[:5])
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Description
        if desc_escaped:
            parts.append("")
            parts.append(desc_escaped)

        # Footer
        parts.append("")
        parts.append(f"{source_emoji} <i>Source: {html_escape(source_text)}</i>")

        # Links
        if book.get("info_link"):
            parts.append(f'<a href="{html_escape(book["info_link"])}">📚 More Info</a>')
        if book.get("goodreads_url"):
            parts.append(f'<a href="{html_escape(book["goodreads_url"])}">Goodreads Page</a>')

        return "\n".join(parts)

    # ── Search helpers ─────────────────────────────────────────────────────────
    async def _preload_hardcover_ratings_for_page(
        self, books: list, page_num: int, page_size: int
    ) -> None:
        """Preload Hardcover ratings for visible books on one page (concurrent).

        Uses asyncio.gather() with one asyncio.to_thread() task per visible
        book so the 5 lookups run genuinely in parallel. Hardcover is the
        SINGLE source of truth for list ratings. Uses the existing _hc_cache
        (no new cache layer).
        """
        start_idx = (page_num - 1) * page_size
        end_idx = min(page_num * page_size, len(books))
        visible = books[start_idx:end_idx]

        logger.info(f"Normal search Hardcover preload started: page={page_num}, books={len(visible)}")

        tasks = [
            asyncio.to_thread(
                MultiSourceBookAggregator._get_hardcover_cached,
                book.get("isbn") or "",
                book.get("title") or "",
                book.get("author") or "",
            )
            for book in visible
        ]
        results = await asyncio.gather(*tasks)

        for book, (hc_rating, hc_count, hc_genres, hc_cover) in zip(visible, results):
            title = book.get("title") or ""
            author = book.get("author") or ""
            isbn = book.get("isbn") or ""

            if hc_rating > 0:
                book["search_rating"] = hc_rating
                book["search_rating_count"] = hc_count
                book["search_rating_formatted"] = f"{hc_rating:.2f}"
                book["_hardcover_match"] = {
                    "title": title,
                    "author": author,
                    "isbn": isbn,
                    "rating": hc_rating,
                    "rating_count": hc_count,
                    "categories": hc_genres,
                    "cover_url": hc_cover,
                }
                logger.info(
                    f"Normal search Hardcover rating: {title} -> {hc_rating:.2f} ({hc_count} ratings)"
                )
            else:
                logger.info(f"Normal search Hardcover rating unavailable: {title}")

        logger.info(f"Normal search Hardcover preload completed: page={page_num}")

    def _build_search_results_message(
        self, books: list, query_text: str, user_id: int, page_num: int, page_size: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build formatted search results message with full titles, authors, ratings, and pagination."""
        total_pages = max(1, (len(books) + page_size - 1) // page_size)
        start_idx = (page_num - 1) * page_size
        end_idx = min(page_num * page_size, len(books))

        parts = [
            f"📚 <b>Search Results</b>",
            f"🔎 <i>{html_escape(query_text)}</i>",
            "",
        ]

        for i in range(start_idx, end_idx):
            book = books[i]
            parts.append(f"📖 <b>{i + 1}.</b> {html_escape(book['title'])}")
            parts.append(f"   ✍️ {html_escape(book['author'])}")

            # Show rating if available (list-only fields, no extra API calls)
            if book.get("search_rating") is not None and book.get("search_rating", 0) > 0:
                parts.append(
                    f"   ⭐ {book.get('search_rating_formatted', 'N/A')} ({book['search_rating_count']:,} ratings)"
                )
            else:
                parts.append("   ❓ No ratings yet")

            parts.append("")  # blank line between entries

        # Footer
        if total_pages > 1:
            parts.append(f"👇 <i>Select a book — Page {page_num} of {total_pages}</i>")
        else:
            parts.append("👇 <i>Select a book:</i>")

        # Keyboard: compact numbered buttons in rows of 5 + pagination nav
        keyboard = []
        row = []
        for i in range(start_idx, end_idx):
            row.append(InlineKeyboardButton(str(i + 1), callback_data=f"book_{user_id}_{i}_{page_num}"))
            if len(row) == 5:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        if total_pages > 1:
            nav_row = []
            if page_num > 1:
                nav_row.append(InlineKeyboardButton("◀️", callback_data=f"page_{user_id}_{page_num - 1}"))
            nav_row.append(InlineKeyboardButton(f"{page_num}/{total_pages}", callback_data="noop"))
            if page_num < total_pages:
                nav_row.append(InlineKeyboardButton("▶️", callback_data=f"page_{user_id}_{page_num + 1}"))
            keyboard.append(nav_row)

        return "\n".join(parts), InlineKeyboardMarkup(keyboard)

    # ── Search ────────────────────────────────────────────────────────────────
    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /search command."""
        try:
            if not context.args:
                await update.message.reply_text(
                    "Please provide a book title or author name.\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            query_text = " ".join(context.args)

            if not query_text or len(query_text.strip()) < 2:
                await update.message.reply_text(
                    "Please provide a valid search query (at least 2 characters).\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            await update.message.chat.send_action("typing")
            logger.info(f"👤 User search: {query_text}")

            # Get aggregator results first, then preload Hardcover ratings for visible books.
            # Hardcover is the single source of truth for list ratings; each book on the
            # current page is looked up individually via _get_hardcover_cached (cached, concurrent).
            books = await self.aggregator.aggregate_book_data(query_text, limit=10)

            if not books:
                logger.warning(f"No books found for: {query_text}")
                await update.message.reply_text(
                    f"❌ <b>No books found</b> for '<b>{html_escape(query_text)}</b>'\n\n"
                    "<i>Try different keywords or check spelling.</i>",
                    parse_mode=ParseMode.HTML,
                )
                return

            user_id = update.effective_user.id
            self._set_cached_books(user_id, books)
            self._search_page_cache[user_id] = 1
            self._search_query_cache[user_id] = query_text

            # Preload Hardcover ratings for page 1 BEFORE building the UI.
            # Hardcover must be available so ratings appear in the result list.
            # _preload_hardcover_ratings_for_page is async; internally it runs concurrent
            # Hardcover lookups via asyncio.gather() inside asyncio.to_thread().
            await self._preload_hardcover_ratings_for_page(books, 1, 5)
            logger.info("Normal search result list building AFTER Hardcover preload")
            results_text, keyboard = self._build_search_results_message(
                books, query_text, user_id, 1, 5
            )
            await update.message.reply_text(results_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Error in search_command: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ An error occurred while searching.", parse_mode=ParseMode.HTML
            )

    # ── Button callbacks ────────────────────────────────────────────────────────

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all inline button presses."""
        try:
            query = update.callback_query
            await query.answer()
            callback_data = query.data

            # ── Pagination ───────────────────────────────────────────────────
            if callback_data.startswith("page_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])
                page_num = int(parts[2])

                books = self._get_cached_books(user_id)
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                self._search_page_cache[user_id] = page_num
                query_text = self._search_query_cache.get(user_id, "")

                # Preload Hardcover ratings for this page BEFORE rebuilding the message.
                # Hardcover must be available so ratings appear in the list.
                # _preload_hardcover_ratings_for_page is async; internally it runs concurrent
                # Hardcover lookups via asyncio.gather() inside asyncio.to_thread().
                await self._preload_hardcover_ratings_for_page(books, page_num, 5)
                logger.info(f"Building result list page {page_num} AFTER Hardcover preload")
                results_text, keyboard = self._build_search_results_message(
                    books, query_text, user_id, page_num, 5
                )
                await query.edit_message_text(
                    text=results_text, reply_markup=keyboard, parse_mode=ParseMode.HTML
                )
                return

            # ── Back to results ────────────────────────────────────────────────
            if callback_data.startswith("back_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])

                books = self._get_cached_books(user_id)
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                page_num = self._search_page_cache.get(user_id, 1)
                query_text = self._search_query_cache.get(user_id, "")
                results_text, keyboard = self._build_search_results_message(
                    books, query_text, user_id, page_num=page_num, page_size=5
                )

                # Delete the current message (could be a photo or text) and send
                # a clean, fresh text-only message with the results list.
                await query.delete_message()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=results_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
                return

            # ── Download cover ────────────────────────────────────────────────
            if callback_data.startswith("download_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])
                book_idx = int(parts[2])

                books = self._get_cached_books(user_id)
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                if book_idx >= len(books):
                    await query.answer("Invalid selection.", show_alert=True)
                    return

                book = books[book_idx]
                cover_url = book.get("cover_url")

                if not cover_url:
                    await query.answer("No cover image available.", show_alert=True)
                    return

                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url, book)
                if not temp_file:
                    await query.answer("Failed to download cover.", show_alert=True)
                    return

                try:
                    with open(temp_file, "rb") as f:
                        await context.bot.send_document(
                            chat_id=query.message.chat_id,
                            document=f,
                            filename=f"{book['title'][:40].replace(' ', '_')}_cover.jpg",
                            caption=f"📖 <b>{html_escape(book['title'])}</b>\n{html_escape(book['author'])}",
                            parse_mode=ParseMode.HTML,
                        )
                finally:
                    self.cleanup_temp_file(temp_file)
                return

            # ── Hourglass button (inline details expansion) ───────────────────
            if callback_data.startswith("hourglass_"):
                # Extract callback key from hourglass_<callback_key>
                callback_key = callback_data[10:]  # Remove "hourglass_" prefix

                # Get book data from callback cache
                book_data = self._get_inline_callback_data(callback_key)
                if not book_data:
                    await query.answer("Book data expired.", show_alert=True)
                    return

                # Ensure we have complete data (blocking I/O, run in thread to avoid blocking event loop)
                book_data, _ = await asyncio.to_thread(MultiSourceBookAggregator._ensure_ratings, book_data)
                book_data = await asyncio.to_thread(MultiSourceBookAggregator._ensure_cover, book_data, hc_data=_)

                # Fill missing bibliographic fields (ISBN, pages, year) by looking up
                # the book on Hardcover.  This only runs when at least one of those
                # fields is absent (e.g. iTunes-only inline results) and only for the
                # ONE book the user selected — it does not affect inline search speed.
                if not book_data.get("isbn") or not book_data.get("page_count") or not book_data.get("published_date"):
                    hc_books = await asyncio.to_thread(
                        MultiSourceBookAggregator.search_hardcover,
                        f"{book_data.get('title', '')} {book_data.get('author', '')}".strip(),
                        5,
                    )
                    if hc_books:
                        # Pick best match by title+author similarity, preferring metadata-rich results
                        title_lower = (book_data.get("title") or "").lower()
                        author_lower = (book_data.get("author") or "").lower()

                        def _metadata_richness(book: dict) -> int:
                            """Score how much useful metadata a Hardcover result has."""
                            score = 0
                            if book.get("isbn"): score += 2
                            if book.get("page_count"): score += 2
                            if book.get("published_date"): score += 2
                            if book.get("categories") or book.get("genres"): score += 2
                            if book.get("description"): score += 1
                            if book.get("cover_url"): score += 1
                            if book.get("rating") and book.get("rating") > 0: score += 1
                            if book.get("rating_count") and book.get("rating_count") > 0: score += 1
                            return score

                        best, best_score, best_richness = None, -1, -1
                        for hb in hc_books:
                            hb_title = (hb.get("title") or "").lower()
                            hb_author = (hb.get("author") or "").lower()
                            # Primary: title match (2) + author match (2) = max 4
                            title_match = 2 if (title_lower in hb_title or hb_title in title_lower) else 0
                            author_match = 2 if (author_lower in hb_author or hb_author in author_lower) else 0
                            primary_score = title_match + author_match
                            # Secondary: metadata richness (max 12)
                            richness = _metadata_richness(hb)
                            # Combined: primary dominates, richness breaks ties
                            combined = (primary_score << 8) + richness
                            if combined > best_score:
                                best_score = combined
                                best_richness = richness
                                best = hb
                        if best and best_score >= 256:  # At least one of title/author matched
                            if not book_data.get("isbn") and best.get("isbn"):
                                book_data["isbn"] = best["isbn"]
                            if not book_data.get("page_count") and best.get("page_count"):
                                book_data["page_count"] = best["page_count"]
                            if not book_data.get("published_date") and best.get("published_date"):
                                book_data["published_date"] = best["published_date"]
                            # Also merge genres/categories, description, cover, and rating
                            # if the selected book is missing them
                            if not book_data.get("categories") and best.get("categories"):
                                book_data["categories"] = best["categories"]
                                book_data["genres"] = best.get("genres", best["categories"])
                            if not book_data.get("description") and best.get("description"):
                                book_data["description"] = best["description"]
                            if not book_data.get("cover_url") and best.get("cover_url"):
                                book_data["cover_url"] = best["cover_url"]
                            if not book_data.get("rating") and best.get("rating"):
                                book_data["rating"] = best["rating"]
                                book_data["rating_count"] = best.get("rating_count", 0)
                                book_data["rating_formatted"] = best.get("rating_formatted", f"{best['rating']:.2f}")
                                book_data["rating_source"] = best.get("rating_source", "hardcover")

                # Build expanded caption
                expanded_caption = self._build_expanded_inline_caption(book_data)

                # Build Goodreads keyboard
                gr_keyboard = self._build_goodreads_keyboard(book_data)

                # Restore cover image: the inline Article produces a text-only
                # message, so convert it into a photo message carrying the cover
                # and the expanded caption in a single call.
                cover_url = book_data.get("cover_url")
                if cover_url and not cover_url.startswith("data:"):
                    try:
                        media = InputMediaPhoto(
                            media=cover_url,
                            caption=expanded_caption,
                            parse_mode=ParseMode.HTML,
                        )
                        await query.edit_message_media(
                            media=media,
                            reply_markup=gr_keyboard,
                        )
                        logger.info(
                            f"Inline details expanded (photo) for: {book_data.get('title', 'Unknown')}"
                        )
                        return
                    except Exception as media_error:
                        logger.warning(
                            f"Failed to restore photo media, falling back to text: {media_error}"
                        )
                        # Fall through to text-caption / text edits below.

                # Edit the inline message (same cover, new caption and keyboard)
                try:
                    await query.edit_message_caption(
                        caption=expanded_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=gr_keyboard
                    )
                    logger.info(f"Inline details expanded for: {book_data.get('title', 'Unknown')}")
                except Exception as e:
                    logger.warning(f"Failed to edit inline message caption: {e}")
                    # Fallback: try to edit message text if caption edit fails
                    try:
                        await query.edit_message_text(
                            text=expanded_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=gr_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Failed to edit inline message: {e2}")
                        await query.answer("Failed to update message.", show_alert=True)
                return

            # ── Book selection ────────────────────────────────────────────────
            if not callback_data.startswith("book_"):
                return

            parts = callback_data.split("_")
            user_id = int(parts[1])
            book_idx = int(parts[2])
            # page_num is encoded in callback as 4th part (for Back to Results restoration)
            page_num = int(parts[3]) if len(parts) > 3 else 1
            self._search_page_cache[user_id] = page_num

            books = self._get_cached_books(user_id)
            if books is None:
                await query.edit_message_text("❌ Search results expired.", parse_mode=ParseMode.HTML)
                return

            if book_idx >= len(books):
                await query.edit_message_text("❌ Invalid selection.", parse_mode=ParseMode.HTML)
                return

            book = books[book_idx]

            # Fetch ratings lazily (saves Hardcover API quota — not fetched during search).
            # _ensure_ratings also returns the cached Hardcover data so _ensure_cover
            # can reuse it without a redundant API call.
            # Blocking I/O, run in thread to avoid blocking the event loop.
            book, hc_data = await asyncio.to_thread(MultiSourceBookAggregator._ensure_ratings, book)

            # Source a real cover if Google Books only offered its placeholder.
            # Pass hc_data to avoid re-fetching from Hardcover.
            book = await asyncio.to_thread(MultiSourceBookAggregator._ensure_cover, book, hc_data=hc_data)

            # Translate description lazily when user selects a book (performance fix).
            # Skip if description is already English to avoid unnecessary HTTP calls.
            if book.get("description") and not is_english_description(book["description"]):
                book["description"] = await asyncio.to_thread(
                    translate_to_english, book["description"]
                )

            text_info = self.format_book_message(book)

            # Truncate caption to stay within Telegram's 1024 character limit.
            #
            # The caption is HTML, so we can't just slice at a fixed byte offset —
            # truncating mid-<a ...> tag leaves broken markup that Telegram rejects
            # ("Can't parse entities: unsupported start tag ..."). Any text-derived
            # HTML tags (the <a href> links) may sit near the cutoff point, so:
            # 1. Strip tags to get clean plain text
            # 2. Truncate at a word boundary
            # 3. Re-append the Goodreads link (exact, valid HTML) last
            MAX_CAPTION = 950
            if len(text_info) > MAX_CAPTION:
                # Truncate HTML safely without breaking markup or collapsing lines.
                # - Strip tags to "" (the message's real "\n" line breaks stay)
                # - Replace any HTML-coded newlines with plain newlines
                # - Collapse spaces around newlines, but keep the newlines
                plain = re.sub(r"<br\s*/?>", "\n", text_info, flags=re.I)
                plain = re.sub(r"<[^>]+>", "", plain)
                plain = re.sub(r" *\n *", "\n", plain).strip()
                cutoff = plain.rfind(" ", 0, MAX_CAPTION)
                gr_url = build_goodreads_url(book)
                suffix = f'\n\n<a href="{gr_url}">📖 View on Goodreads</a>'
                if cutoff > 0:
                    text_info = plain[:cutoff] + "..." + suffix
                else:
                    text_info = plain[:MAX_CAPTION] + "..." + suffix

            await query.delete_message()

            temp_file = None
            cover_url = book.get("cover_url")

            if cover_url:
                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url, book)

            if temp_file:
                # ── TEMP FILE DIAGNOSTICS ──
                import os as _os
                _fpath = temp_file
                _fsize = _os.path.getsize(_fpath) if _os.path.exists(_fpath) else -1
                _fhex = ""
                _freadable = False
                _fpos_after_open = -1
                _pil_fmt = ""
                _pil_dims = ""
                try:
                    with open(_fpath, "rb") as _tf:
                        _fhex = _tf.read(16).hex()
                        _tf.seek(0)
                        _freadable = len(_tf.read(1)) == 1
                        _tf.seek(0)
                        _fpos_after_open = _tf.tell()
                        # PIL format/dims if already available (Pillow is in requirements.txt)
                        try:
                            from PIL import Image as _PILImg
                            with open(_fpath, "rb") as _pf:
                                _pil_img = _PILImg.open(_pf)
                                _pil_fmt = _pil_img.format or "UNKNOWN"
                                _pil_dims = f"{_pil_img.width}x{_pil_img.height}"
                        except Exception:
                            pass
                except Exception as _e:
                    _fhex = f"<read error: {_e}>"
                logger.info(
                    f"COVER DIAG: path={_fpath} size={_fsize} first16={_fhex} "
                    f"readable={_freadable} fpos={_fpos_after_open} "
                    f"pil_fmt={_pil_fmt} pil_dims={_pil_dims}"
                )
                # ── END DIAGNOSTICS ──

                try:
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "📥 Download Cover",
                                callback_data=f"download_{user_id}_{book_idx}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Back to Results",
                                callback_data=f"back_{user_id}",
                            )
                        ],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    logger.info(f"ABOUT TO SEND COVER: path={_fpath} size={_fsize} position={_fpos_after_open}")
                    with open(temp_file, "rb") as f:
                        await context.bot.send_photo(
                            chat_id=query.message.chat_id,
                            photo=f,
                            caption=text_info,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
                    logger.info("COVER SEND COMPLETED")
                    logger.info(f"✅ Sent book: {book['title']}")
                except Exception as e:
                    logger.warning(f"Could not send photo: {e}")
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "🔙 Back to Results",
                                callback_data=f"back_{user_id}",
                            )
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text_info,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                    )
                finally:
                    self.cleanup_temp_file(temp_file)
            else:
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🔙 Back to Results",
                            callback_data=f"back_{user_id}",
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text_info,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )

            # ── Hourglass button (inline details expansion) ─────────────────────
            if callback_data.startswith("hourglass_"):
                # Extract callback key from hourglass_<callback_key>
                callback_key = callback_data[10:]  # Remove "hourglass_" prefix

                # Get book data from callback cache
                book_data = self._get_inline_callback_data(callback_key)
                if not book_data:
                    await query.answer("Book data expired.", show_alert=True)
                    return

                # Ensure we have complete data (blocking I/O, run in thread to avoid blocking event loop)
                book_data, _ = await asyncio.to_thread(MultiSourceBookAggregator._ensure_ratings, book_data)
                book_data = await asyncio.to_thread(MultiSourceBookAggregator._ensure_cover, book_data, hc_data=_)

                # Build expanded caption
                expanded_caption = self._build_expanded_inline_caption(book_data)

                # Build Goodreads keyboard
                gr_keyboard = self._build_goodreads_keyboard(book_data)

                # Edit the inline message (same cover, new caption and keyboard)
                try:
                    await query.edit_message_caption(
                        caption=expanded_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=gr_keyboard
                    )
                    logger.info(f"Inline details expanded for: {book_data.get('title', 'Unknown')}")
                except Exception as e:
                    logger.warning(f"Failed to edit inline message caption: {e}")
                    # Fallback: try to edit message text if caption edit fails
                    try:
                        await query.edit_message_text(
                            text=expanded_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=gr_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Failed to edit inline message: {e2}")
                        await query.answer("Failed to update message.", show_alert=True)

        except Exception as e:
            logger.error(f"Error in button_callback: {e}", exc_info=True)
            await query.answer("❌ An error occurred", show_alert=True)

    # ── Expanded inline helpers ────────────────────────────────────────────────

    def _build_expanded_inline_caption(self, book: dict) -> str:
        """Build expanded caption for when hourglass button is pressed."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        isbn = html_escape(book.get("isbn", ""))
        pages = book.get("page_count", 0)
        published_date = book.get("published_date", "")
        year = published_date[:4] if published_date else ""
        lang = book.get("language", "")
        publisher = html_escape(book.get("publisher", ""))
        asin = book.get("asin", "")

        # Start with required header
        parts = [
            f"📖 <b>Title:</b> {title}",
            f"✍️ <b>Author:</b> {author}",
            "",  # blank line
        ]

        # Genres: limit to 5, remove duplicates
        categories = book.get("categories", [])
        if categories:
            # Remove duplicates while preserving order
            seen = set()
            unique_categories = []
            for cat in categories:
                if cat not in seen:
                    seen.add(cat)
                    unique_categories.append(cat)
            # Limit to 5
            limited_categories = unique_categories[:5]
            genres_str = ", ".join(limited_categories)
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Rating
        rating = book.get("rating_formatted") or book.get("rating")
        rating_cnt = book.get("rating_count", 0)
        rating_reviews = book.get("rating_reviews", 0)
        if rating and rating_cnt:
            try:
                rating_num = float(str(rating).replace(",", "."))
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(
                    f"⭐ <b>Rating:</b> {stars} <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )
            except ValueError:
                parts.append(
                    f"⭐ <b>Rating:</b> {html_escape(str(rating))}/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )

        # Core metadata that should NOT be removed
        if isbn:
            parts.append(f"🆔 <b>ISBN:</b> <code>{isbn}</code>")
        if pages:
            parts.append(f"📄 <b>Pages:</b> {pages}")
        if year:
            parts.append(f"📅 <b>Year:</b> {year}")

        # Optional lower-priority fields
        if lang:
            parts.append(f"🌐 <b>Language:</b> {html_escape(lang)}")
        if publisher:
            parts.append(f"🏢 <b>Publisher:</b> {publisher}")

        parts.append("")  # blank line before summary

        # Description with expandable blockquote - will be truncated first if needed
        desc_text = (book.get("description") or "").strip()
        if desc_text:
            # Clean HTML tags
            desc_text = re.sub(r"<[^>]+>", "", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            # Escape for HTML
            desc_text = html_escape(desc_text)
            parts.append("📄 <b>Summary</b>")
            parts.append(f"<blockquote expandable>{desc_text}</blockquote>")

        parts.append("")
        parts.append("🔵 <b>Source:</b> Hardcover")

        # Join and ensure length is safe
        caption = "\n".join(parts)

        # Final safety check - if still too long, truncate description FIRST
        # (never remove core metadata: ISBN, Pages, Year)
        if len(caption) > 1020:
            # Find description parts
            for i, part in enumerate(parts):
                if part == "📄 <b>Summary</b>":
                    if i + 1 < len(parts) and parts[i + 1].startswith("<blockquote expandable>"):
                        current_desc = parts[i + 1][23:-13]
                        parts_without_desc = parts[:i+1] + [""] + parts[i+2:]
                        base_length = len("\n".join(parts_without_desc))
                        max_desc_len = 1020 - base_length - 3
                        if max_desc_len > 10:
                            if len(current_desc) > max_desc_len:
                                truncated = current_desc[:max_desc_len]
                                last_space = truncated.rfind(" ")
                                if last_space > max_desc_len * 0.8:
                                    truncated = truncated[:last_space]
                                parts[i + 1] = f"<blockquote expandable>{truncated}...</blockquote>"
                    break
            caption = "\n".join(parts)

        return caption

    def _build_goodreads_keyboard(self, book: dict) -> InlineKeyboardMarkup:
        """Build keyboard with Goodreads button for expanded view."""
        gr_url = build_goodreads_url(book)
        keyboard = [[InlineKeyboardButton("📚 Open Goodreads 🔗", url=gr_url)]]
        return InlineKeyboardMarkup(keyboard)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self):
        """Start the bot with long polling."""
        logger.info("=" * 80)
        logger.info("🚀 Starting Multi-Source Book Bot")
        logger.info("=" * 80)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        self.app.run_polling()


# ── Vercel singleton (survives warm starts) ─────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")

_bot_instance: GoodreadsBot | None = None


def get_bot() -> GoodreadsBot:
    """Get or create the global bot instance (singleton for warm starts)."""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = GoodreadsBot(TELEGRAM_BOT_TOKEN, webhook_mode=True)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(_bot_instance.app.initialize())
    return _bot_instance