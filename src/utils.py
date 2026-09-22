"""Shared utilities — logging, escaping, cover-image validation, constants."""

import logging
import hashlib
import struct

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Realistic browser headers ──────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

# ── HTML / Markdown helpers ─────────────────────────────────────────────────────
from telegram import helpers


def md_escape(text: str) -> str:
    """Escape characters for Telegram MarkdownV2."""
    return helpers.escape_markdown(text, version=2)


def html_escape(text: str) -> str:
    """Escape characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Cover‑image validation ──────────────────────────────────────────────────────
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
    """True if the bytes are Google Books' 'image not available' placeholder."""
    if not content or len(content) < 100:
        return True
    if hashlib.md5(content).hexdigest() in _GB_PLACEHOLDER_MD5:
        return True
    # The placeholder is a PNG at one of a few fixed sizes; real covers are JPEG.
    if _png_dimensions(content) in _GB_PLACEHOLDER_PNG_SIZES:
        return True
    return False


def is_unreliable_gb_cover(volume_id: str) -> bool:
    """Google Books volume IDs ending in 'AACAAJ' are metadata‑only records
    with no cover art — their image URLs resolve to the placeholder."""
    return bool(volume_id) and volume_id.endswith("AACAAJ")


# ── Auto-translation ──────────────────────────────────────────────────────────
# Translates non-English text to English using Google Translate's free API.
# Uses the same requests library already imported — no extra dependencies.
# Handles 429 rate limits with a single retry after a short delay.

import requests as _requests


def translate_to_english(text: str) -> str:
    """Translate *text* to English. Returns original on failure."""
    if not text or not text.strip():
        return text
    # Chunk long text to avoid URL length limits and API truncation.
    # ~500 chars is a safe chunk size; Google Translate returns incomplete
    # translations for very long strings.
    CHUNK_SIZE = 480
    if len(text) > CHUNK_SIZE:
        chunks = [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
        translated_chunks = []
        for chunk in chunks:
            result = _translate_chunk(chunk)
            translated_chunks.append(result)
            if result != chunk:
                pass  # translated OK
        return "".join(translated_chunks)
    return _translate_chunk(text)


def _translate_chunk(text: str) -> str:
    """Translate a single short chunk. Internal — always called by the public fn."""
    for attempt in range(2):
        try:
            resp = _requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code == 429:
                if attempt == 0:
                    import time

                    time.sleep(1.5)
                    continue
                logger.warning("Translation failed: status=429 (rate-limited)")
                return text
            if resp.status_code != 200:
                logger.warning(f"Translation failed: status={resp.status_code}")
                return text
            # Parse: [[["translated","original",...], ...], lang, ...]
            try:
                data = resp.json()
            except ValueError:
                logger.warning("Translation failed: non-JSON response")
                return text
            if not isinstance(data, list) or not data:
                logger.warning("Translation failed: unexpected response structure")
                return text
            sentences = data[0]
            if not isinstance(sentences, list):
                logger.warning("Translation failed: unexpected response structure [sentences]")
                return text
            parts = []
            for part in sentences:
                if isinstance(part, list) and len(part) > 0 and part[0]:
                    parts.append(part[0])
            translated = "".join(parts)
            return translated if translated else text
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            return text
    return text