# Modular Refactoring Plan

## Goal
Split `goodreads_bot_advanced.py` (1340 lines) into clean, focused modules — no logic changes, just better organization.

## Goodreads URL Fix
In the caption truncation block (when caption > 950 chars), replace the placeholder text with a real Goodreads link:
- If book has `goodreads_url` → use it directly
- Else if book has `isbn` → `https://www.goodreads.com/book/isbn/{isbn}`
- Else → `https://www.goodreads.com/search?q={title}+{author}`

## New File Structure
```
src/
  __init__.py       # package marker (already exists)
  utils.py          # md_escape, html_escape, HEADERS, is_placeholder_image, etc.
  search.py         # _polite_get, scrape_goodreads, _extract_json_ld, _parse_json_ld, etc.
  aggregator.py     # MultiSourceBookAggregator class + _hc_cache
  handlers.py       # GoodreadsBot class (all command/button handlers)
goodreads_bot.py    # NEW entry point — creates and runs GoodreadsBot
goodreads_bot_advanced.py  # KEPT as backup until new version is verified
```

## Module Breakdown

| File | Contents | Approx Lines |
|------|----------|-------------|
| `src/utils.py` | `md_escape`, `html_escape`, `HEADERS`, `is_placeholder_image`, `is_unreliable_gb_cover`, `logger` | ~100 |
| `src/search.py` | `scrape_goodreads`, `_polite_get`, `_extract_json_ld`, `_parse_json_ld`, `_html_fallback`, `_GR_HEADERS` | ~170 |
| `src/aggregator.py` | `MultiSourceBookAggregator` class + `_hc_cache` dict (all search/enrich methods) | ~490 |
| `src/handlers.py` | `GoodreadsBot` class — `format_book_message`, `search_command`, `button_callback`, download helpers, `run` | ~380 |
| `goodreads_bot.py` | 3 lines: load env, create bot, run | ~3 |

## Order of Changes
1. Apply Goodreads URL fix to `goodreads_bot_advanced.py` (current working file)
2. Split code into `src/` modules (no logic changes)
3. Create `goodreads_bot.py` as the new entry point
4. Verify imports resolve correctly
5. Test `goodreads_bot.py` runs without errors

## Backup
`goodreads_bot_advanced.py` stays untouched until the new structure is verified working.