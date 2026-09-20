"""Entry point for the Multi-Source Book Bot (polling mode)."""

import os
import socket
import threading

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")

from src.handlers import GoodreadsBot  # noqa: E402, F401

# ── Tiny web server (keep-alive) ─────────────────────────────────────────────
# Koyeb's free tier sleeps an instance after ~30 min of no traffic. A Telegram
# polling bot only outbound-polls, so it generates no traffic and would go to
# sleep. We expose a trivial /ping route and let UptimeRobot (or any external
# cron) hit it every ~20 min to keep the instance awake.

PORT = int(os.getenv("PORT", "8080"))


def _run_webserver():
    from flask import Flask, Response

    app = Flask(__name__)

    @app.route("/ping")
    def ping():
        return Response("ok", status=200, mimetype="text/plain")

    @app.route("/health")
    def health():
        return Response("ok", status=200, mimetype="text/plain")

    # TLS isn't needed on Koyeb (it terminates at the edge). Run plain HTTP.
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def _start_webserver():
    """Launch the Flask keep-alive server on a background daemon thread."""
    t = threading.Thread(target=_run_webserver, daemon=True, name="webserver")
    t.start()
    # Confirm the port actually bound so we fail fast if it's taken.
    for _ in range(25):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", PORT))
            s.close()
            break
        except OSError:
            s.close()
            time.sleep(0.2)


import time  # noqa: E402

_start_webserver()

bot = GoodreadsBot(TELEGRAM_BOT_TOKEN)
bot.run()