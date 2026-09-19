import logging
import requests
from bs4 import BeautifulSoup
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram import helpers
import html
import time
from urllib.parse import quote
import tempfile
import re
import json
import asyncio
import random
import hashlib
import struct

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_BOOKS_API_KEY = os.getenv('GOOGLE_BOOKS_API_KEY')

# Realistic browser headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")

logger.info("✅ Bot configuration loaded successfully")

def md_escape(text: str) -> str:
    """Escape characters for Telegram MarkdownV2."""
    return helpers.escape_markdown(text, version=2)


def html_escape(text: str) -> str:
    """Escape characters for Telegram HTML parse mode."""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;"))

# ----------------------------------------------------------------------
# Cover‑image validation
# ----------------------------------------------------------------------
# Google Books serves an "image not available" placeholder for catalog‑only
# records (volume IDs ending in "AACAAJ" — metadata with no scanned content).
# At the zoom level this bot downloads, that placeholder is a byte‑identical
# PNG of a fixed size, while genuine covers come back as JPEG — so it can be
# detected and rejected instead of being sent to the user as a real cover.
_GB_PLACEHOLDER_MD5 = {"a64fa89d7ebc97075c1d363fc5fea71f"}
_GB_PLACEHOLDER_PNG_SIZES = {(575, 750), (300, 391)}


def _png_dimensions(data: bytes):
    """Return (width, height) for PNG bytes, else None."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", data[16:24])
        except Exception:
            return None
    return None


def is_placeholder_image(content: bytes) -> bool:
    """True if the bytes are Google Books' "image not available" placeholder."""
    if not content or len(content) < 100:
        return True
    if hashlib.md5(content).hexdigest() in _GB_PLACEHOLDER_MD5:
        return True
    # The placeholder is a PNG at one of a few fixed sizes; real covers are JPEG.
    if _png_dimensions(content) in _GB_PLACEHOLDER_PNG_SIZES:
        return True
    return False


def is_unreliable_gb_cover(volume_id: str) -> bool:
    """Google Books volume IDs ending in "AACAAJ" are metadata‑only records
    with no cover art — their image URLs resolve to the placeholder."""
    return bool(volume_id) and volume_id.endswith("AACAAJ")


# ----------------------------------------------------------------------
# Goodreads scraping helper (JSON‑LD primary, HTML fallback, rate‑limited)
# ----------------------------------------------------------------------
MIN_REQUEST_INTERVAL = 2.0      # seconds between any two requests
JITTER_RANGE        = (0.5, 1.5) # extra random delay added each request
_GR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}
_last_request_ts = 0.0   # module‑level timestamp for rate‑limit


def _polite_get(url: str):
    """GET with rate‑buffer + jitter; returns Response or None on failure."""
    global _last_request_ts
    now = time.time()
    elapsed = now - _last_request_ts
    wait = max(0.0, MIN_REQUEST_INTERVAL - elapsed) + random.uniform(*JITTER_RANGE)
    if wait > 0:
        time.sleep(wait)

    try:
        resp = requests.get(url, headers=_GR_HEADERS, timeout=12)
        _last_request_ts = time.time()
        if resp.status_code == 200:
            return resp
        # 429 or 503 → back off a bit more and retry once
        if resp.status_code in (429, 503, 502, 504):
            time.sleep(5.0 + random.uniform(0, 2))
            resp2 = requests.get(url, headers=_GR_HEADERS, timeout=12)
            _last_request_ts = time.time()
            if resp2.status_code == 200:
                return resp2
    except Exception:
        pass
    return None


def _extract_json_ld(soup: BeautifulSoup) -> dict:
    """Pull structured data from the first JSON‑LD block on the page."""
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        # Goodreads uses @type "Book"
        if isinstance(data, dict) and data.get("@type") == "Book":
            return data
        # Sometimes it's a list
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Book":
                    return item
    return {}


def _parse_json_ld(data: dict) -> dict:
    """Map JSON‑LD fields to the bot’s internal dict."""
    out = {}
    out["title"] = data.get("name", "").strip()
    # Authors may be a dict or list of dicts
    authors = data.get("author")
    if isinstance(authors, list):
        out["author"] = ", ".join(
            a.get("name", "").strip() for a in authors if isinstance(a, dict)
        )
    elif isinstance(authors, dict):
        out["author"] = authors.get("name", "").strip()
    # Rating
    agg = data.get("aggregateRating")
    if isinstance(agg, dict):
        out["rating"] = float(agg.get("ratingValue", 0))
        out["rating_count"] = int(agg.get("ratingCount", 0))
    # Description
    out["description"] = (data.get("description") or "").strip()
    # ISBN – useful for cover fallback
    out["isbn"] = data.get("isbn", "")
    # Publication date
    out["published_date"] = data.get("datePublished", "")
    # Number of pages
    out["page_count"] = data.get("numberOfPages", 0)
    return out


def _html_fallback(soup: BeautifulSoup, isbn: str) -> dict:
    """Classic HTML scraping when JSON‑LD is missing/incomplete."""
    out = {"isbn": isbn}
    # Title & author – goodreads uses specific classes / itemprop
    title_tag = soup.find("h1", {"data-testid": "bookTitle"})
    if title_tag:
        out["title"] = title_tag.get_text(strip=True)
    author_tag = soup.find("span", {"data-testid": "name"})
    if author_tag:
        out["author"] = author_tag.get_text(strip=True)

    # Rating
    rating_tag = soup.find("div", {"data-testid": "ratingValue"})
    if rating_tag:
        try:
            out["rating"] = float(rating_tag.get_text(strip=True))
        except ValueError:
            pass
    count_tag = soup.find("div", {"data-testid": "ratingCount"})
    if count_tag:
        m = re.search(r"[\d,]+", count_tag.get_text())
        if m:
            out["rating_count"] = int(m.group().replace(",", ""))

    # Description – often hidden behind a spoiler; take the first <div data-testid="description">
    desc_tag = soup.find("div", {"data-testid": "description"})
    if desc_tag:
        out["description"] = desc_tag.get_text(separator=" ", strip=True)

    # Cover – try to get the largest image from the page
    img_tag = soup.find("img", {"data-testid": "bookCover"})
    if img_tag and img_tag.get("src"):
        out["cover_url"] = img_tag["src"].replace("._SY160_", "").replace("._SY475_", "")
    # If still missing, try to construct from ISBN via Goodreads image service
    if not out.get("cover_url") and out.get("isbn"):
        out["cover_url"] = f"https://images.gr-assets.com/books/1405398843l/{out['isbn']}.jpg"
    return out


def scrape_goodreads(book_title: str, author: str = None) -> dict | None:
    """
    Main entry point – given a title (and optional author) returns a dict with
    the fields the bot expects, or None if scraping failed.
    """
    # Build a search query – Goodreads search URL format:
    query = f"{book_title} {author or ''}".strip()
    search_url = f"https://www.goodreads.com/search?q={requests.utils.quote(query)}"
    resp = _polite_get(search_url)
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")

    # Grab the first result link
    first_result = soup.find("a", {"class": "bookTitle"})
    if not first_result or not first_result.get("href"):
        return None
    book_url = "https://www.goodreads.com" + first_result["href"]

    # Now fetch the actual book page
    book_resp = _polite_get(book_url)
    if not book_resp:
        return None
    book_soup = BeautifulSoup(book_resp.text, "html.parser")

    # 1️⃣ Try JSON‑LD first
    json_ld = _extract_json_ld(book_soup)
    if json_ld:
        data = _parse_json_ld(json_ld)
        # Ensure we have at least a title & author
        if data.get("title") and data.get("author"):
            return data

    # 2️⃣ Fallback to HTML selectors
    isbn = json_ld.get("isbn", "")
    data = _html_fallback(book_soup, isbn)
    if data.get("title") and data.get("author"):
        return data
    return None


class MultiSourceBookAggregator:
    """Aggregates book data from multiple sources for best quality"""

    @staticmethod
    def search_google_books(query, limit=10):
        """Search Google Books API"""
        try:
            logger.info(f"🔍 Searching Google Books: {query}")
            url = 'https://www.googleapis.com/books/v1/volumes'

            params = {
                'q': query,
                'maxResults': min(limit, 40),
                'printType': 'books',
                'orderBy': 'relevance',
                'langRestrict': 'en'
            }

            if GOOGLE_BOOKS_API_KEY:
                params['key'] = GOOGLE_BOOKS_API_KEY

            response = requests.get(url, params=params, timeout=15)
            if response.status_code != 200:
                logger.warning(f"Google Books API error: {response.status_code}")
                return []

            data = response.json()
            items = data.get('items', [])

            books = []
            for item in items:
                book = MultiSourceBookAggregator._parse_google_book(item)
                if book:
                    books.append(book)

            logger.info(f"✅ Google Books: found {len(books)} books")
            return books

        except Exception as e:
            logger.error(f"Google Books error: {e}")
            return []

    @staticmethod
    def _parse_google_book(item):
        """Parse Google Books item"""
        try:
            vol = item.get('volumeInfo', {})
            volume_id = item.get('id', '')

            title = vol.get('title', '').strip()
            if not title:
                return None

            authors = vol.get('authors', [])
            author = authors[0] if authors else 'Unknown Author'

            # Get rating
            rating = vol.get('averageRating', 0.0)
            rating_count = vol.get('ratingsCount', 0)

            # Get ISBN
            isbn = ''
            for id_obj in vol.get('industryIdentifiers', []):
                if id_obj.get('type') in ['ISBN_13', 'ISBN_10']:
                    isbn = id_obj.get('identifier', '')
                    break

            # Get cover (all sizes)
            img = vol.get('imageLinks', {})
            cover_url = (
                img.get('extraLarge') or
                img.get('large') or
                img.get('medium') or
                img.get('thumbnail') or
                img.get('smallThumbnail', '')
            )
            if cover_url:
                cover_url = cover_url.replace('http://', 'https://').replace('&zoom=1', '&zoom=0')
            # Catalog-only Google Books records (volume IDs ending in "AACAAJ")
            # only ever return the "image not available" placeholder — treat them
            # as having no cover so a real one is sourced later (Open Library/iTunes).
            if is_unreliable_gb_cover(volume_id):
                cover_url = ''
            # Google Books "content" URLs are frequently the placeholder image;
            # drop them here so a real cover is sourced later (iTunes → Hardcover
            # → Open Library) when the user actually selects this book.
            if cover_url and 'books.googleapis.com/books/content?id=' in cover_url:
                cover_url = ''

            return {
                'title': title,
                'author': author,
                'rating': rating,
                'rating_count': rating_count,
                'description': vol.get('description', ''),
                'isbn': isbn,
                'cover_url': cover_url,
                'page_count': vol.get('pageCount', 0),
                'published_date': vol.get('publishedDate', ''),
                'categories': vol.get('categories', []),
                'info_link': vol.get('infoLink', ''),
                'gb_volume_id': volume_id,
                'source': 'google_books',
                # Store raw volumeInfo for later enrichment
                '_raw_volume_info': vol
            }
        except Exception as e:
            logger.debug(f"Error parsing Google book: {e}")
            return None

    @staticmethod
    def search_itunes(query):
        """Search iTunes API for high-quality covers"""
        try:
            logger.info(f"🔍 Searching iTunes: {query}")
            url = "https://itunes.apple.com/search"
            params = {
                "term": query,
                "media": "ebook",
                "entity": "ebook",
                "limit": 10
            }

            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return []

            data = response.json()
            results = data.get('results', [])

            books = []
            for result in results:
                book = MultiSourceBookAggregator._parse_itunes_book(result)
                if book:
                    books.append(book)

            logger.info(f"✅ iTunes: found {len(books)} books")
            return books

        except Exception as e:
            logger.error(f"iTunes error: {e}")
            return []

    @staticmethod
    def _parse_itunes_book(result):
        """Parse iTunes result"""
        try:
            title = result.get('trackName', '').strip()
            if not title:
                return None

            author = result.get('artistName', 'Unknown Author')

            # Get highest quality artwork
            artwork_url = result.get('artworkUrl100', '')
            if artwork_url:
                # Upgrade to maximum resolution
                artwork_url = artwork_url.replace('100x100', '2048x2048')
                artwork_url = artwork_url.replace('60x60', '2048x2048')

            return {
                'title': title,
                'author': author,
                'cover_url': artwork_url,
                'description': result.get('description', ''),
                'source': 'itunes'
            }
        except Exception as e:
            logger.debug(f"Error parsing iTunes book: {e}")
            return None

    @staticmethod
    def aggregate_book_data(query, limit=10):
        """
        Aggregate book data from multiple sources and combine best information

        Strategy:
        1. Search Google Books and iTunes in parallel
        2. For each Google Books result, try to find a matching iTunes result for cover
        3. Keep Google Books as primary source for description, categories, etc.
        4. Ratings and genres will be fetched lazily from Hardcover/StoryGraph on selection.
        """
        logger.info(f"📚 Aggregating data from multiple sources for: {query}")

        # Search all sources
        google_books = MultiSourceBookAggregator.search_google_books(query, limit)
        itunes_books = MultiSourceBookAggregator.search_itunes(query)

        # If no results from any source – try Goodreads scraping as a last resort
        if not google_books and not itunes_books:
            logger.warning("No results from any source – trying Goodreads fallback")
            gr_data = scrape_goodreads(query)
            if gr_data:
                book = {
                    "title": gr_data.get("title", ""),
                    "author": gr_data.get("author", ""),
                    "rating": gr_data.get("rating", 0.0),
                    "rating_count": gr_data.get("rating_count", 0),
                    "description": gr_data.get("description", ""),
                    "cover_url": gr_data.get("cover_url", ""),
                    "isbn": gr_data.get("isbn", ""),
                    "page_count": gr_data.get("page_count", 0),
                    "published_date": str(gr_data.get("published_date", "")),
                    "info_link": f"https://www.goodreads.com/search?q={requests.utils.quote(query)}",
                    "source": "goodreads",
                    "rating_formatted": f"{gr_data.get('rating', 0):.2f}" if gr_data.get('rating') else "N/A",
                }
                logger.info(f"✅ Goodreads fallback found: {book['title']}")
                return [book]
            logger.warning("No results from any source")
            return []

        # Use Google Books as primary source (best descriptions and metadata)
        aggregated_books = []

        for gb_book in google_books[:limit]:
            # Start with Google Books data
            book = gb_book.copy()

            # Try to enhance with iTunes cover (higher quality) – only if confident match
            itunes_match = MultiSourceBookAggregator._find_matching_book_strict(
                book['title'], book['author'], itunes_books
            )
            if itunes_match and itunes_match.get('cover_url'):
                logger.info(f"📸 Using iTunes cover for: {book['title']}")
                book['cover_url'] = itunes_match['cover_url']
                book['cover_source'] = 'itunes'
            else:
                book['cover_source'] = 'google_books'

            # Hide rating in search results; will be fetched lazily on selection
            book['rating'] = 0.0
            book['rating_count'] = 0
            book['rating_formatted'] = "N/A"

            aggregated_books.append(book)

        # If Google Books had no results, use iTunes as primary
        if not aggregated_books and itunes_books:
            for itunes_book in itunes_books[:limit]:
                book = itunes_book.copy()
                book['cover_source'] = 'itunes'

                # Format rating
                if book['rating'] > 0:
                    book['rating_formatted'] = f"{book['rating']:.2f}"
                else:
                    book['rating_formatted'] = "N/A"
                aggregated_books.append(book)

        logger.info(f"✅ Aggregated {len(aggregated_books)} books with enhanced data")
        return aggregated_books

    @staticmethod
    def _find_matching_book_strict(title, author, book_list):
        """Find matching book in list by title/author similarity with stricter threshold."""
        title_lower = title.lower()
        author_lower = author.lower()

        for book in book_list:
            book_title = book.get('title', '').lower()
            book_author = book.get('author', '').lower()

            # Check if titles match (fuzzy) – require high similarity
            if (title_lower in book_title or book_title in title_lower or
                MultiSourceBookAggregator._similarity(title_lower, book_title) > 0.8):
                # Check if authors match – require high similarity
                if (author_lower in book_author or book_author in author_lower or
                    MultiSourceBookAggregator._similarity(author_lower, book_author) > 0.8):
                    return book

        return None

    @staticmethod
    def _similarity(s1, s2):
        """Enhanced string similarity that handles author name variations"""
        try:
            # Normalize strings: lowercase, remove extra spaces
            import re
            norm_s1 = re.sub(r'\s+', ' ', s1.strip().lower())
            norm_s2 = re.sub(r'\s+', ' ', s2.strip().lower())

            # Basic Jaccard similarity on words
            words1 = set(norm_s1.split())
            words2 = set(norm_s2.split())
            intersection = words1.intersection(words2)
            union = words1.union(words2)
            jaccard_sim = len(intersection) / len(union) if union else 0

            # Also check for substring containment (good for "J.R.R. Tolkien" in "J. R. R. Tolkien")
            substr_sim = 0
            if norm_s1 in norm_s2 or norm_s2 in norm_s1:
                substr_sim = 1.0
            # Also try without spaces around periods
            norm_s1_no_spaces = norm_s1.replace(' ', '')
            norm_s2_no_spaces = norm_s2.replace(' ', '')
            if norm_s1_no_spaces in norm_s2_no_spaces or norm_s2_no_spaces in norm_s1_no_spaces:
                substr_sim = 1.0

            # Return the best match
            return max(jaccard_sim, substr_sim)
        except:
            return 0

    @staticmethod
    def _get_hardcover_data(isbn: str, title: str = '', author: str = '') -> tuple:
        """Fetch book data from Hardcover.app GraphQL API.

        Returns tuple: (rating, ratings_count, genres, image_url)
        Uses search with query_type: "Book" and selects the hit with highest ratings_count.
        """
        api_key = os.getenv('HARDCOVER_API_KEY', '').strip()
        if not api_key:
            return 0.0, 0, [], ''

        try:
            # Build search query - prefer title+author for accuracy
            if title and author:
                search_query = f"{title} {author}".strip()
            elif title:
                search_query = title.strip()
            elif isbn:
                search_query = isbn.replace('-', '').strip()
            else:
                return 0.0, 0, [], ''

            if not search_query:
                return 0.0, 0, [], ''

            query = """
            query SearchBooks($q: String!, $limit: Int) {
                search(query: $q, query_type: "Book", per_page: $limit) {
                    ids
                    results
                }
            }
            """
            variables = {"q": search_query, "limit": 10}
            resp = requests.post(
                'https://api.hardcover.app/v1/graphql',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={"query": query, "variables": variables},
                timeout=10
            )
            if resp.status_code != 200:
                logger.debug(f"Hardcover GraphQL error {resp.status_code}: {resp.text[:200]}")
                return 0.0, 0, [], ''

            data = resp.json()
            if 'errors' in data:
                logger.debug(f"Hardcover GraphQL errors: {data['errors']}")
                return 0.0, 0, [], ''

            search_result = data.get('data', {}).get('search', {})
            raw_results = search_result.get('results', {})
            # results is a JSONB object, may be string or dict
            results_json = json.loads(raw_results) if isinstance(raw_results, str) else raw_results
            hits = results_json.get('hits', [])

            if not hits:
                return 0.0, 0, [], ''

            # Determine if we have title/author to filter by
            title_lower = title.lower() if title else ""
            author_lower = author.lower() if author else ""
            have_title = bool(title_lower)
            have_author = bool(author_lower)

            # Helper to check if a hit matches the title/author
            def _matches_title_author(doc):
                doc_title = doc.get('title', '').lower()
                doc_authors = doc.get('author_names', [])
                if isinstance(doc_authors, list):
                    doc_authors_str = ' '.join(doc_authors).lower()
                else:
                    doc_authors_str = str(doc_authors).lower()

                title_match = (not have_title) or (title_lower and title_lower in doc_title)
                author_match = (not have_author) or (author_lower and author_lower in doc_authors_str)
                return title_match and author_match

            # First, try to find hits that match the title/author
            if have_title or have_author:
                filtered_hits = [h for h in hits if _matches_title_author(h.get('document', {}))]
                if filtered_hits:
                    best_hit = max(filtered_hits, key=lambda h: h.get('document', {}).get('ratings_count') or 0)
                    logger.debug(f"Hardcover: selected from {len(filtered_hits)} title/author matches")
                else:
                    # Fallback to highest ratings_count among all hits if no title/author match
                    best_hit = max(hits, key=lambda h: h.get('document', {}).get('ratings_count') or 0)
                    logger.debug(f"Hardcover: no title/author match, falling back to highest ratings_count among {len(hits)} hits")
            else:
                # No title/author to filter by, just take the highest ratings_count
                best_hit = max(hits, key=lambda h: h.get('document', {}).get('ratings_count') or 0)
                logger.debug(f"Hardcover: no title/author filter, selecting highest ratings_count among {len(hits)} hits")

            doc = best_hit.get('document', {})

            rating = doc.get('rating') or 0.0
            ratings_count = doc.get('ratings_count') or 0
            genres = doc.get('genres', []) or []
            image_data = doc.get('image', {}) or {}
            image_url = image_data.get('url', '')

            if rating and ratings_count:
                logger.info(f"⭐ Hardcover.app: {rating}/5 from {ratings_count} ratings for '{title or isbn}'")
                return round(float(rating), 2), int(ratings_count), genres, image_url
            else:
                logger.debug(f"Hardcover hit has no rating: rating={rating}, count={ratings_count}")
                return 0.0, 0, [], ''

        except Exception as e:
            logger.debug(f"Hardcover.app data lookup failed: {e}")
            return 0.0, 0, [], ''

    @staticmethod
    def _get_storygraph_ratings(isbn: str, title: str = '', author: str = '') -> tuple:
        """Fetch ratings from StoryGraph API.

        Uses the storygraph-api package: pip install storygraph-api
        API docs: https://pypi.org/project/storygraph-api/
        """
        try:
            from storygraph import Storygraph
            sg = Storygraph()
            if isbn:
                result = sg.get_book(isbn=isbn)
            else:
                result = sg.search_books(query=f"{title} {author}".strip())
                if result and len(result) > 0:
                    result = result[0]
                else:
                    return 0.0, 0

            if not result:
                return 0.0, 0

            avg = result.get('rating') or result.get('avg_rating') or 0
            count = result.get('rating_count') or result.get('num_ratings') or 0

            if avg and count:
                logger.info(f"⭐ StoryGraph: {avg}/5 from {count} ratings")
                return round(float(avg), 2), int(count)
        except ImportError:
            logger.debug("storygraph-api not installed: pip install storygraph-api")
        except Exception as e:
            logger.debug(f"StoryGraph rating lookup failed: {e}")
        return 0.0, 0

    @staticmethod
    def _ensure_ratings(book: dict) -> dict:
        """Ensure book has ratings by fetching from Hardcover/StoryGraph if missing.

        Args:
            book: Dictionary with book data (must have 'title', 'author', optionally 'isbn')

        Returns:
            Updated book dictionary with ratings filled in if available
        """
        # If already has a rating, return as-is
        if book.get('rating') and book.get('rating') > 0:
            return book

        # Try Hardcover first (cached to avoid repeat API calls)
        hc_rating, hc_count, hc_genres, hc_cover = MultiSourceBookAggregator._get_hardcover_cached(
            book.get('isbn', ''), book.get('title', ''), book.get('author', '')
        )
        if hc_rating > 0:
            book['rating'] = hc_rating
            book['rating_count'] = hc_count
            book['rating_source'] = 'hardcover'
            book['rating_formatted'] = f"{hc_rating:.2f}"
            # Store Hardcover genres and cover for later use
            if hc_genres:
                book['categories'] = hc_genres
            if hc_cover and not book.get('cover_url'):
                book['cover_url'] = hc_cover
                book['cover_source'] = 'hardcover'
            return book

        # Fallback to StoryGraph
        sg_rating, sg_count = MultiSourceBookAggregator._get_storygraph_ratings(
            book.get('isbn', ''), book.get('title', ''), book.get('author', '')
        )
        if sg_rating > 0:
            book['rating'] = sg_rating
            book['rating_count'] = sg_count
            book['rating_source'] = 'storygraph'
            book['rating_formatted'] = f"{sg_rating:.2f}"

        return book

    @staticmethod
    def _get_openlibrary_cover(isbn: str) -> str:
        """Return a real Open Library cover URL for an ISBN, or '' if none.

        Open Library's ``?default=false`` responds 404 when it has no cover, so
        a 200 carrying a genuine (non-placeholder) image means success.
        """
        if not isbn:
            return ''
        clean = isbn.replace('-', '').strip()
        if not clean:
            return ''
        check_url = f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg?default=false"
        try:
            r = requests.get(check_url, headers=HEADERS, timeout=10)
            if (r.status_code == 200 and len(r.content) > 3000
                    and not is_placeholder_image(r.content)):
                logger.info(f"🖼️ Open Library cover found for ISBN {clean}")
                return f"https://covers.openlibrary.org/b/isbn/{clean}-L.jpg"
        except Exception as e:
            logger.debug(f"Open Library cover lookup failed: {e}")
        return ''

    @staticmethod
    def _ensure_cover(book: dict) -> dict:
        """Ensure the book has a real cover image.

        Google Books placeholder covers are dropped during parsing, so this
        fills a missing cover — priority: iTunes (high quality) → Hardcover → Open Library.
        Called lazily when the user selects a book so searches stay fast.
        """
        if book.get('cover_url'):
            return book  # already have a usable (non-placeholder) cover

        # 1) iTunes by title/author (highest quality artwork)
        try:
            title = book.get('title', '')
            author = book.get('author', '')
            itunes = MultiSourceBookAggregator.search_itunes(f"{title} {author}".strip())
            match = MultiSourceBookAggregator._find_matching_book_strict(title, author, itunes)
            if match and match.get('cover_url'):
                book['cover_url'] = match['cover_url']
                book['cover_source'] = 'itunes'
                logger.info(f"🖼️ iTunes cover found for: {title}")
                return book
        except Exception as e:
            logger.debug(f"iTunes cover lookup failed: {e}")

        # 2) Hardcover by title/author/isbn (good quality, community-driven)
        try:
            title = book.get('title', '')
            author = book.get('author', '')
            isbn = book.get('isbn', '')
            _, _, _, hc_cover = MultiSourceBookAggregator._get_hardcover_data(isbn, title, author)
            if hc_cover:
                book['cover_url'] = hc_cover
                book['cover_source'] = 'hardcover'
                logger.info(f"🖼️ Hardcover cover found for: {title}")
                return book
        except Exception as e:
            logger.debug(f"Hardcover cover lookup failed: {e}")

        # 3) Open Library by ISBN — reliable, 404s when it has no cover
        ol = MultiSourceBookAggregator._get_openlibrary_cover(book.get('isbn', ''))
        if ol:
            book['cover_url'] = ol
            book['cover_source'] = 'open_library'
            logger.info(f"🖼️ Open Library cover found for ISBN {book.get('isbn', '')}")
            return book

        return book

    # ── Hardcover rating cache ─────────────────────────────────────────────────
    # Simple in-memory cache so repeated lookups for the same book don't waste API calls.
    # Key = (norm_isbn, norm_title, norm_author), Value = (rating, count, genres, cover_url)
    _hc_cache: dict = {}

    @staticmethod
    def _get_hardcover_cached(isbn: str, title: str, author: str) -> tuple:
        """Cached wrapper around _get_hardcover_data.

        Cache is keyed by (isbn, title, author) so the same book looked up
        multiple times (e.g. same ISBN in GB + iTunes results) hits cache.
        """
        norm = (
            isbn.replace('-', '').strip().lower() if isbn else '',
            title.strip().lower() if title else '',
            author.strip().lower() if author else '',
        )
        if norm in MultiSourceBookAggregator._hc_cache:
            logger.debug(f"🔁 Hardcover cache hit: {title or isbn}")
            return MultiSourceBookAggregator._hc_cache[norm]

        result = MultiSourceBookAggregator._get_hardcover_data(isbn, title, author)

        # Cap cache size to bound memory usage (drop oldest entries)
        if len(MultiSourceBookAggregator._hc_cache) >= 512:
            for old_key in list(MultiSourceBookAggregator._hc_cache.keys())[:64]:
                MultiSourceBookAggregator._hc_cache.pop(old_key, None)

        MultiSourceBookAggregator._hc_cache[norm] = result
        return result

    @staticmethod
    def _flush_hc_cache():
        """Clear the Hardcover cache (call at start of new day or manually)."""
        MultiSourceBookAggregator._hc_cache.clear()

    # ── Hardcover cover fallback ─────────────────────────────────────────────
    @staticmethod
    def _get_hardcover_cover(isbn: str = '', title: str = '', author: str = '') -> str:
        """Fetch cover image URL from Hardcover.app as a fallback.

        Delegates to the cached unified lookup (_get_hardcover_data) so it shares
        the same single API call as ratings/genres. Returns a URL or ''.
        """
        try:
            _, _, _, cover_url = MultiSourceBookAggregator._get_hardcover_cached(
                isbn, title, author
            )
            return cover_url or ''
        except Exception as e:
            logger.debug(f"Hardcover cover lookup error: {e}")
            return ''


class GoodreadsBot:
    def __init__(self, token, webhook_mode=False):
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
        self.search_cache = {}
        self.aggregator = MultiSourceBookAggregator()
        self.setup_handlers()
        if webhook_mode:
            # Initialize application for webhook mode (don't start polling)
            pass

    def setup_handlers(self):
        """Setup all command and query handlers"""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("search", self.search_command))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_error_handler(self.error_handler)

    def process_update(self, raw_update: dict) -> bool:
        """Process a single update dict received from Telegram webhook.
        Returns True if processed, False otherwise.
        """
        import asyncio
        from telegram import Update as TGUpdate

        try:
            update = TGUpdate.de_json(raw_update, self.app.bot)
            loop = None
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # run the update through the app — errors re-raised by error_handler
            # are captured in the Future result; we check for them explicitly
            coro = self.app.process_update(update)
            future = asyncio.ensure_future(coro)
            loop.run_until_complete(future)

            # If the handler raised an exception, it will be set as the Future result
            result = future.result()
            if isinstance(result, Exception):
                logger.error(f"Handler raised: {result}")
                return False
            return True

        except Exception as e:
            logger.error(f"Error processing update: {e}", exc_info=True)
            return False

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Central error handler.

        python-telegram-bot automatically retries the polling loop after a
        transient NetworkError/TimedOut, so we log those briefly instead of
        dumping a full traceback.
        """
        err = context.error
        if isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"Transient network error (auto-retrying): {err!r}")
            return
        logger.error("Unhandled exception while processing update:", exc_info=err)
        raise err  # Re-raise so process_update() can return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a message when /start is issued"""
        welcome_text = """
🤖 <b>Multi-Source Book Bot</b>

Welcome! I search across multiple sources to find the best book information, covers, and descriptions.

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
        """
        await update.message.reply_text(welcome_text.strip(), parse_mode=ParseMode.HTML)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a message when /help is issued"""
        help_text = """
<b>📚 Multi-Source Book Bot Help</b>

<b>Commands:</b>
/start - Show welcome message
/help - Show this help message
/search &lt;query&gt; - Search for books

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
        """
        await update.message.reply_text(help_text.strip(), parse_mode=ParseMode.HTML)

    def download_and_save_image(self, cover_url):
        """Download image from URL and save to temp file"""
        try:
            logger.info(f"📥 Downloading cover: {cover_url[:60]}...")
            response = requests.get(cover_url, headers=HEADERS, timeout=15)
            response.raise_for_status()

            # Safety net: never present Google's "image not available"
            # placeholder as a real cover — send the book as text instead.
            if is_placeholder_image(response.content):
                logger.warning("⚠️ Cover resolved to a placeholder image; skipping cover.")
                return None

            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            temp_file.write(response.content)
            temp_file.close()

            logger.info(f"✅ Downloaded: {len(response.content)} bytes")
            return temp_file.name

        except Exception as e:
            logger.error(f"Error downloading image: {e}")
            return None

    def cleanup_temp_file(self, file_path):
        """Delete temporary file"""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up: {e}")

    def format_book_message(self, book):
        """Format book info for display using Telegram HTML."""
        title   = html_escape(book.get('title', 'Unknown'))
        author  = html_escape(book.get('author', 'Unknown'))
        rating  = html_escape(str(book.get('rating_formatted', book.get('rating', 'N/A'))))
        rating_cnt = book.get('rating_count', 0)
        isbn    = html_escape(book.get('isbn', ''))
        pages   = str(book.get('page_count', 0))
        year    = book.get('published_date', '')[:4]
        desc    = (book.get('description') or '').strip()

        # Clean HTML tags from description
        desc = re.sub(r'<[^>]+>', '', desc)
        desc = re.sub(r'\s+', ' ', desc).strip()

        # Truncate to ~800 chars to stay within limits
        if len(desc) > 800:
            desc = desc[:800] + '...'

        desc_escaped = html_escape(desc)

        # Source badge
        gr_enhanced = book.get('gr_enhanced', False)
        cover_source = book.get('cover_source', book.get('source', 'unknown'))
        source_emoji = {
            'google_books': '🔵', 'open_library': '🟢',
            'itunes': '🟠', 'goodreads': '🔴'
        }.get(cover_source, '⚪')
        source_text = 'Goodreads' if gr_enhanced else cover_source.replace('_', ' ').title()

        parts = []

        # Header
        parts.append(f"<b>{title}</b>")
        parts.append(f"<i>{author}</i>")

        # Rating line with stars
        if rating_cnt > 0:
            try:
                rating_for_float = str(book.get('rating', '')).replace(',', '.')
                rating_num = float(rating_for_float)
                stars = '⭐' * min(int(rating_num), 5)
                parts.append(f"{stars} <b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
            except ValueError:
                parts.append(f"<b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
        else:
            parts.append("❓ <b>No ratings yet</b>")

        parts.append("")  # blank line

        # Metadata in a compact block
        if isbn:
            parts.append(f"📖 <b>ISBN:</b> <code>{isbn}</code>")
        if pages and int(pages) > 0:
            parts.append(f"📄 <b>Pages:</b> <code>{pages}</code>")
        if year:
            parts.append(f"📅 <b>Year:</b> <code>{year}</code>")

        # Genres
        categories = book.get('categories', [])
        if categories:
            genres_str = ', '.join(categories[:5])
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Description
        if desc_escaped:
            parts.append("")
            parts.append(desc_escaped)

        # Footer
        parts.append("")
        parts.append(f"{source_emoji} <i>Source: {html_escape(source_text)}</i>")

        # Links
        if book.get('info_link'):
            parts.append(f'<a href="{html_escape(book["info_link"])}">📚 More Info</a>')
        if book.get('goodreads_url'):
            parts.append(f'<a href="{html_escape(book["goodreads_url"])}">Goodreads Page</a>')

        return '\n'.join(parts)

    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /search command"""
        try:
            if not context.args:
                await update.message.reply_text(
                    "Please provide a book title or author name.\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML
                )
                return

            query = ' '.join(context.args)

            if not query or len(query.strip()) < 2:
                await update.message.reply_text(
                    "Please provide a valid search query (at least 2 characters).\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML
                )
                return

            await update.message.chat.send_action("typing")
            logger.info(f"👤 User search: {query}")

            # Aggregate from multiple sources
            books = self.aggregator.aggregate_book_data(query, limit=10)

            if not books:
                logger.warning(f"No books found for: {query}")
                await update.message.reply_text(
                    f"❌ <b>No books found</b> for '<b>{html_escape(query)}</b>'\n\n"
                    "<i>Try different keywords or check spelling.</i>",
                    parse_mode=ParseMode.HTML
                )
                return

            user_id = update.effective_user.id
            self.search_cache[user_id] = books

            keyboard = []
            for idx, book in enumerate(books[:10]):
                button_text = f"{idx + 1}. {book['title'][:50]}..."
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"book_{user_id}_{idx}")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            results_text = (
                f"📚 <b>Found {len(books)} results</b>\n"
                "<i>Data aggregated from multiple sources</i>\n\n"
                "Select a book:"
            )

            await update.message.reply_text(results_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Error in search_command: {e}", exc_info=True)
            await update.message.reply_text("❌ An error occurred while searching.", parse_mode=ParseMode.HTML)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        try:
            query = update.callback_query
            await query.answer()
            callback_data = query.data

            # Handle back to results
            if callback_data.startswith("back_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])

                if user_id not in self.search_cache:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                books = self.search_cache[user_id]
                keyboard = []
                for idx, book in enumerate(books[:10]):
                    button_text = f"{idx + 1}. {book['title'][:50]}..."
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=f"book_{user_id}_{idx}")])

                reply_markup = InlineKeyboardMarkup(keyboard)
                results_text = f"📚 <b>Found {len(books)} results</b>\n\nSelect a book:"

                await query.edit_message_caption(
                    caption=results_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                return

            # Handle download cover
            if callback_data.startswith("download_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])
                book_idx = int(parts[2])

                if user_id not in self.search_cache:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                books = self.search_cache[user_id]
                if book_idx >= len(books):
                    await query.answer("Invalid selection.", show_alert=True)
                    return

                book = books[book_idx]
                cover_url = book.get('cover_url')

                if not cover_url:
                    await query.answer("No cover image available.", show_alert=True)
                    return

                temp_file = self.download_and_save_image(cover_url)
                if not temp_file:
                    await query.answer("Failed to download cover.", show_alert=True)
                    return

                try:
                    with open(temp_file, 'rb') as f:
                        await context.bot.send_document(
                            chat_id=query.message.chat_id,
                            document=f,
                            filename=f"{book['title'][:40].replace(' ', '_')}_cover.jpg",
                            caption=f"📖 <b>{html_escape(book['title'])}</b>\n{html_escape(book['author'])}",
                            parse_mode=ParseMode.HTML
                        )
                finally:
                    self.cleanup_temp_file(temp_file)
                return

            # Handle book selection
            if not callback_data.startswith("book_"):
                return

            parts = callback_data.split("_")
            user_id = int(parts[1])
            book_idx = int(parts[2])

            if user_id not in self.search_cache:
                await query.edit_message_text("❌ Search results expired.", parse_mode=ParseMode.HTML)
                return

            books = self.search_cache[user_id]
            if book_idx >= len(books):
                await query.edit_message_text("❌ Invalid selection.", parse_mode=ParseMode.HTML)
                return

            book = books[book_idx]

            # Fetch ratings lazily now that the user picked this book
            # (saves Hardcover API quota — not fetched during search anymore)
            book = MultiSourceBookAggregator._ensure_ratings(book)

            # Source a real cover if Google Books only offered its
            # "image not available" placeholder
            book = MultiSourceBookAggregator._ensure_cover(book)

            text_info = self.format_book_message(book)

            await query.delete_message()

            temp_file = None
            cover_url = book.get('cover_url')

            if cover_url:
                temp_file = self.download_and_save_image(cover_url)

            if temp_file:
                try:
                    keyboard = [
                        [InlineKeyboardButton("📥 Download Cover", callback_data=f"download_{user_id}_{book_idx}")],
                        [InlineKeyboardButton("🔙 Back to Results", callback_data=f"back_{user_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    with open(temp_file, 'rb') as f:
                        await context.bot.send_photo(
                            chat_id=query.message.chat_id,
                            photo=f,
                            caption=text_info,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                    logger.info(f"✅ Sent book: {book['title']}")
                except Exception as e:
                    logger.warning(f"Could not send photo: {e}")
                    keyboard = [[InlineKeyboardButton("🔙 Back to Results", callback_data=f"back_{user_id}")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text_info,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                finally:
                    self.cleanup_temp_file(temp_file)
            else:
                keyboard = [[InlineKeyboardButton("🔙 Back to Results", callback_data=f"back_{user_id}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text_info,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )

        except Exception as e:
            logger.error(f"Error in button_callback: {e}", exc_info=True)
            await query.answer("❌ An error occurred", show_alert=True)

    def run(self):
        """Start the bot"""
        logger.info("=" * 80)
        logger.info("🚀 Starting Multi-Source Book Bot")
        logger.info("=" * 80)

        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        self.app.run_polling()



# ── Global bot instance (survives Vercel warm starts) ──────────────────────
import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
_bot_instance = None

def get_bot() -> GoodreadsBot:
    """Get or create the global bot instance (singleton for warm starts)."""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = GoodreadsBot(TELEGRAM_BOT_TOKEN, webhook_mode=True)
        # Application must be initialized before process_update() is called
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(_bot_instance.app.initialize())
    return _bot_instance
