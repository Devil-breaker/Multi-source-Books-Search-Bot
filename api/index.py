"""
api/index.py — Vercel Python entrypoint / router.

Routes requests to the appropriate handler:
  /api/webhook  → Telegram webhook (POST)
  /api/cron     → Heartbeat + cache flush (POST from Vercel Cron)
  /             → simple landing / health page
  anything else → 404

Handlers are imported lazily inside the function so this module loads even if
the `api` package path isn't on sys.path during the Vercel build step.
"""

import json
import sys
import os

# Make the project root importable regardless of CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def handler(request):
    """Main entrypoint — routes based on path."""
    path = (request.path or "/").rstrip("/") or "/"

    if path == "/api/webhook":
        from api.webhook import handler as webhook_handler
        return webhook_handler(request)
    elif path == "/api/cron":
        from api.cron import handler as cron_handler
        return cron_handler(request)
    elif path == "/":
        body = json.dumps(
            {
                "ok": True,
                "name": "Multi-Source Books Search Bot",
                "endpoints": {"/api/webhook": "Telegram webhook (POST)", "/api/cron": "heartbeat + cache flush"},
            }
        )
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": body}
    else:
        return {"statusCode": 404, "body": json.dumps({"error": "Not Found"})}