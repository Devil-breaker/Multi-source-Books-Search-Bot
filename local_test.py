# local_test.py — Local test server for Vercel bot via ngrok
# Run: py -3.12 local_test.py
# Then: ngrok http 8080
# Then: set webhook via browser: https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<ngrok-id>.ngrok.io/api/webhook
"""
Local test server that mimics Vercel's request format.
Telegram sends a JSON body; this server passes it to bot.process_update().
"""
import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
load_dotenv()

from src.handlers import get_bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/webhook":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            raw_update = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request")
            return

        bot = get_bot()
        bot.process_update(raw_update)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):
        logger.info(format % args)


if __name__ == "__main__":
    port = 8080
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"✅ Local webhook server running on http://localhost:{port}")
    logger.info(f"   Expose with: ngrok http {port}")
    logger.info(f"   Then set Telegram webhook to: https://<ngrok-id>.ngrok.io/api/webhook")
    logger.info("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped.")
        server.shutdown()