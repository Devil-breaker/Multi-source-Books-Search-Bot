"""Simple JSON-file cache that survives Vercel cold starts."""
import json, os, time
from pathlib import Path

_CACHE_DIR = os.getenv("CACHE_DIR", "/tmp")
_CACHE_TTL = 3600  # 1 hour


def _path(user_id: int) -> Path:
    return Path(_CACHE_DIR) / f"books_cache_{user_id}.json"


def get_books(user_id: int) -> list | None:
    """Return cached books for user, or None if missing/expired."""
    p = _path(user_id)
    print(f"[CACHE GET] user={user_id} path={p} exists={p.exists()}")
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        age = time.time() - data.get("_ts", 0)
        print(f"[CACHE GET] age={age:.1f}s ttl={_CACHE_TTL}s ok={age <= _CACHE_TTL}")
        if age > _CACHE_TTL:
            p.unlink(missing_ok=True)
            return None
        return data.get("books")
    except Exception as e:
        print(f"[CACHE GET] error={e}")
        return None


def set_books(user_id: int, books: list) -> None:
    """Cache books for a user."""
    p = _path(user_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"_ts": time.time(), "books": books}))
        print(f"[CACHE SET] user={user_id} path={p} books={len(books)}")
    except Exception as e:
        print(f"[CACHE SET] error={e}")