"""GoodreadsBot — all Telegram command and callback handlers."""

import asyncio
import os
import re
import tempfile
import time
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut

from src.utils import logger, HEADERS, html_escape, is_placeholder_image
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
        self._SEARCH_CACHE_MAX: int = 1000      # max users tracked
        self.aggregator = MultiSourceBookAggregator()
        self.webhook_mode = webhook_mode
        self.setup_handlers()

    def _get_cached_books(self, user_id: int) -> list | None:
        """Return cached search results for *user_id*, or None if missing/expired."""
        entry = self.search_cache.get(user_id)
        if entry is None:
            return None
        books, ts = entry
        if time.time() - ts > self._SEARCH_CACHE_TTL:
            self.search_cache.pop(user_id, None)
            return None
        return books

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
        self.search_cache[user_id] = (books, time.time())

    # ── Handler registration ──────────────────────────────────────────────────

    def setup_handlers(self):
        """Register all command and callback handlers."""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("search", self.search_command))
        self.app.add_handler(CommandHandler("ping", self.ping_command))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_error_handler(self.error_handler)

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
        await update.message.reply_text(
            """
🤖 <b>Multi-Source Book Bot</b>

Welcome! I search across multiple sources to find the best book information,
covers, and descriptions.

<b>How to use:</b>
• <code>/search &lt;book_title&gt;</code> - Search for books

<b>Example:</b>
<code>/search Harry Potter and the Prisoner of Azkaban</code>

<b>Data Sources:</b>
📚 Google Books - Descriptions &amp; metadata
🍎 iTunes - High-resolution covers
💠 Hardcover.app - Community ratings
📖 StoryGraph - Social reading ratings

Use /help for more information.
            """.strip(),
            parse_mode=ParseMode.HTML,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help text on /help."""
        await update.message.reply_text(
            """
<b>📚 Multi-Source Book Bot Help</b>

<b>Commands:</b>
/start - Show welcome message
/help - Show this help message
/search &lt;query&gt; - Search for books
/ping - Check if the bot is running

<b>Features:</b>
✓ Searches multiple sources simultaneously
✓ Combines best data from each source
✓ High-resolution covers from iTunes
✓ Descriptions from Google Books
✓ Community ratings from Hardcover.app
✓ Social ratings from StoryGraph
✓ Download covers as image files

<b>Tips:</b>
• Use full book titles for best results
• Include author name for better matching
• Try different keywords if no results
            """.strip(),
            parse_mode=ParseMode.HTML,
        )

    async def ping_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reply with bot status on /ping."""
        await update.message.reply_text("✅ Bot is running and polling Telegram!")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def download_and_save_image(self, cover_url: str):
        """Download cover image and save to a temp file. Returns path or None."""
        try:
            logger.info(f"📥 Downloading cover: {cover_url[:60]}...")
            response = requests.get(cover_url, headers=HEADERS, timeout=15)
            response.raise_for_status()

            if is_placeholder_image(response.content):
                logger.warning("⚠️ Cover resolved to a placeholder image; skipping cover.")
                return None

            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            temp_file.write(response.content)
            temp_file.close()
            logger.info(f"✅ Downloaded: {len(response.content)} bytes")
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

            keyboard = []
            for idx, book in enumerate(books[:10]):
                button_text = f"{idx + 1}. {book['title'][:50]}..."
                keyboard.append(
                    [InlineKeyboardButton(button_text, callback_data=f"book_{user_id}_{idx}")]
                )

            reply_markup = InlineKeyboardMarkup(keyboard)
            results_text = (
                f"📚 <b>Found {len(books)} results</b>\n"
                "<i>Data aggregated from multiple sources</i>\n\n"
                "Select a book:"
            )

            await update.message.reply_text(results_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Error in search_command: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ An error occurred while searching.", parse_mode=ParseMode.HTML
            )

    # ── Button callbacks ──────────────────────────────────────────────────────

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all inline button presses."""
        try:
            query = update.callback_query
            await query.answer()
            callback_data = query.data

            # ── Back to results ────────────────────────────────────────────────
            if callback_data.startswith("back_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])

                books = self._get_cached_books(user_id)
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                keyboard = []
                for idx, book in enumerate(books[:10]):
                    button_text = f"{idx + 1}. {book['title'][:50]}..."
                    keyboard.append(
                        [InlineKeyboardButton(button_text, callback_data=f"book_{user_id}_{idx}")]
                    )

                reply_markup = InlineKeyboardMarkup(keyboard)
                results_text = f"📚 <b>Found {len(books)} results</b>\n\nSelect a book:"

                # Delete the current message (could be a photo or text) and send
                # a clean, fresh text-only message with the results list.
                await query.delete_message()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=results_text,
                    reply_markup=reply_markup,
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

                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url)
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

            # ── Book selection ────────────────────────────────────────────────
            if not callback_data.startswith("book_"):
                return

            parts = callback_data.split("_")
            user_id = int(parts[1])
            book_idx = int(parts[2])

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
            book, hc_data = MultiSourceBookAggregator._ensure_ratings(book)

            # Source a real cover if Google Books only offered its placeholder.
            # Pass hc_data to avoid re-fetching from Hardcover.
            book = MultiSourceBookAggregator._ensure_cover(book, hc_data=hc_data)

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
                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url)

            if temp_file:
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

                    with open(temp_file, "rb") as f:
                        await context.bot.send_photo(
                            chat_id=query.message.chat_id,
                            photo=f,
                            caption=text_info,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
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

        except Exception as e:
            logger.error(f"Error in button_callback: {e}", exc_info=True)
            await query.answer("❌ An error occurred", show_alert=True)

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