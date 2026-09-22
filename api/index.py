"""
api/index.py — Vercel Python entrypoint using Flask.
Flask's `app` is reliably detected by @vercel/python.
"""
import json, sys, os, logging

from flask import Flask, request as flask_request, Response

# Make project root importable
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/api/webhook", methods=["POST"])
def webhook():
    """Receive Telegram updates."""
    try:
        raw_update = json.loads(flask_request.get_data())
    except Exception:
        return Response("Bad Request", status=400)

    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret:
        provided = flask_request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided != secret:
            return Response("Forbidden", status=403)

    from src.handlers import get_bot
    bot = get_bot()
    bot.process_update(raw_update)
    return Response(json.dumps({"ok": True}), status=200, mimetype="application/json")


@app.route("/api/cron", methods=["POST"])
def cron():
    """Vercel cron: flush cache and heartbeat."""
    if not flask_request.headers.get("X-Vercel-Cron"):
        return Response("Forbidden", status=403)

    from src.handlers import get_bot
    from src.aggregator import MultiSourceBookAggregator

    get_bot()
    MultiSourceBookAggregator._flush_hc_cache()
    logger.info("Cron: Hardcover cache flushed")
    return Response(
        json.dumps({"ok": True, "status": "ok"}),
        status=200,
        mimetype="application/json",
    )


@app.route("/")
def root():
    return Response(
        json.dumps({
            "ok": True,
            "name": "Multi-Source Books Search Bot",
            "endpoints": {
                "/api/webhook": "Telegram webhook (POST)",
                "/api/cron": "heartbeat + cache flush",
            },
        }),
        status=200,
        mimetype="application/json",
    )